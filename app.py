import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="Auto Google Form SaaS")

# ==============================================================================
# CẤU HÌNH DỊCH VỤ, ADMIN & WEBHOOK TỰ ĐỘNG CỘNG TIỀN
# ==============================================================================
PRICE_PER_SURVEY = 250  # 250 VNĐ / 1 phiếu khảo sát thành công
ADMIN_USERNAME = "anhquy19"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "anhquy1907")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "quy_secret_2026")  # Mã bảo mật Webhook
DB_PATH = "survey_saas.db"

SESSIONS: Dict[str, str] = {}
TASKS: Dict[str, Dict[str, Any]] = {}
DB_LOCK = threading.Lock()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36",
]

DEFAULT_TEXT_SAMPLES = [
    "Sản phẩm chất lượng tốt trong tầm giá, bảng màu dễ dùng.",
    "Cần cải thiện thêm độ dưỡng ẩm để môi mềm mịn hơn.",
    "Nên duy trì nhiều chương trình ưu đãi trên sàn thương mại điện tử.",
    "Thiết kế bao bì đẹp mắt, nhỏ gọn tiện mang theo.",
]


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with DB_LOCK:
        conn = get_db()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                amount INTEGER NOT NULL,
                tx_type TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_webhooks (
                tx_ref TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )
        existing_admin = conn.execute(
            "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME.lower(),)
        ).fetchone()
        if not existing_admin:
            salt = secrets.token_hex(8)
            pw_hash = hash_password(ADMIN_PASSWORD, salt)
            now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            conn.execute(
                "INSERT INTO users (username, password_hash, salt, balance, created_at) VALUES (?, ?, ?, 0, ?)",
                (ADMIN_USERNAME.lower(), pw_hash, salt, now_str),
            )
        conn.commit()
        conn.close()


init_db()


def adjust_balance(username: str, delta: int, tx_type: str, note: str) -> int:
    with DB_LOCK:
        conn = get_db()
        row = conn.execute("SELECT balance FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            conn.close()
            raise ValueError("Tài khoản không tồn tại.")
        new_balance = row["balance"] + delta
        if new_balance < 0:
            conn.close()
            raise ValueError("Số dư không đủ để thực hiện giao dịch.")
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        conn.execute("UPDATE users SET balance = ? WHERE username = ?", (new_balance, username))
        conn.execute(
            "INSERT INTO transactions (username, amount, tx_type, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, delta, tx_type, note, now_str),
        )
        conn.commit()
        conn.close()
        return new_balance


def get_current_user(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Vui lòng đăng nhập để sử dụng.")
    token = authorization.split(" ", 1)[1].strip()
    username = SESSIONS.get(token)
    if not username:
        raise HTTPException(status_code=401, detail="Phiên đăng nhập hết hạn, vui lòng đăng nhập lại.")
    return username


def get_admin_user(username: str = Depends(get_current_user)) -> str:
    if username != ADMIN_USERNAME.lower():
        raise HTTPException(status_code=403, detail="Chỉ tài khoản Admin mới có quyền này!")
    return username


class AuthRequest(BaseModel):
    username: str
    password: str


class ScanRequest(BaseModel):
    url: str


class RunRequest(BaseModel):
    view_url: str
    submit_url: str
    page_history: str
    total: int = 10
    delay_min: float = 1.0
    delay_max: float = 2.5
    questions: List[Dict[str, Any]]


class AdminTopupRequest(BaseModel):
    target_username: str
    amount: int
    note: str = "Admin nạp tiền thủ công"


# ==============================================================================
# WEBHOOK TỰ ĐỘNG CỘNG TIỀN (SEPAY / CASSO)
# ==============================================================================
@app.post("/api/webhook/payment")
async def api_payment_webhook(request: Request, authorization: Optional[str] = Header(default=None)):
    # Xác thực mã bảo mật từ SePay (Header: Authorization: Apikey <WEBHOOK_SECRET>)
    if WEBHOOK_SECRET:
        expected = f"Apikey {WEBHOOK_SECRET}"
        if authorization != expected and request.query_params.get("secret") != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Sai mã xác thực Webhook.")

    data = await request.json()

    # Hỗ trợ chuẩn dữ liệu SePay (transferType == 'in', transferAmount, content, id/referenceCode)
    transfer_type = str(data.get("transferType", "in")).lower()
    if transfer_type != "in":
        return {"ok": True, "ignored": "Giao dịch tiền ra"}

    amount = int(data.get("transferAmount") or data.get("amount") or 0)
    content = str(data.get("content") or data.get("description") or "")
    tx_ref = str(data.get("id") or data.get("referenceCode") or f"{amount}_{content}_{time.time()}")

    if amount <= 0:
        return {"ok": True, "ignored": "Số tiền không hợp lệ"}

    # Tìm cú pháp: NAP <username> (không phân biệt hoa thường)
    match = re.search(r"\bNAP\s*([a-z0-9_]{3,24})\b", content, re.IGNORECASE)
    if not match:
        return {"ok": True, "ignored": f"Không tìm thấy mã NAP <username> trong nội dung: {content}"}

    target_username = match.group(1).lower()

    # Chống cộng trùng 1 giao dịch nhiều lần
    with DB_LOCK:
        conn = get_db()
        dup = conn.execute("SELECT tx_ref FROM processed_webhooks WHERE tx_ref = ?", (tx_ref,)).fetchone()
        if dup:
            conn.close()
            return {"ok": True, "ignored": "Giao dịch đã được xử lý trước đó"}
        conn.execute(
            "INSERT INTO processed_webhooks (tx_ref, created_at) VALUES (?, ?)",
            (tx_ref, datetime.now().strftime("%d/%m/%Y %H:%M:%S")),
        )
        conn.commit()
        conn.close()

    try:
        new_bal = adjust_balance(
            target_username,
            amount,
            "AUTO_TOPUP",
            f"Nạp tự động qua ngân hàng (Mã GD: {tx_ref})",
        )
        return {"ok": True, "username": target_username, "added": amount, "new_balance": new_bal}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


# ==============================================================================
# API TÀI KHOẢN & SỐ DƯ
# ==============================================================================
@app.post("/api/register")
def api_register(req: AuthRequest):
    uname = req.username.strip().lower()
    if uname == ADMIN_USERNAME.lower():
        raise HTTPException(status_code=400, detail="Tên đăng nhập này đã được hệ thống sử dụng.")
    if not re.match(r"^[a-z0-9_]{3,24}$", uname):
        raise HTTPException(
            status_code=400,
            detail="Tên đăng nhập dài 3-24 ký tự, chỉ gồm chữ thường, số hoặc dấu gạch dưới (_).",
        )
    if len(req.password) < 4:
        raise HTTPException(status_code=400, detail="Mật khẩu phải có ít nhất 4 ký tự.")

    salt = secrets.token_hex(8)
    pw_hash = hash_password(req.password, salt)
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, salt, balance, created_at) VALUES (?, ?, ?, 0, ?)",
                (uname, pw_hash, salt, now_str),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            raise HTTPException(status_code=400, detail="Tên đăng nhập này đã có người sử dụng.")
        conn.close()

    token = secrets.token_hex(24)
    SESSIONS[token] = uname
    return {
        "token": token,
        "username": uname,
        "balance": 0,
        "is_admin": False,
        "price_per_survey": PRICE_PER_SURVEY,
    }


@app.post("/api/login")
def api_login(req: AuthRequest):
    uname = req.username.strip().lower()
    with DB_LOCK:
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (uname,)).fetchone()
        conn.close()

    if not row or hash_password(req.password, row["salt"]) != row["password_hash"]:
        raise HTTPException(status_code=401, detail="Sai tên đăng nhập hoặc mật khẩu.")

    token = secrets.token_hex(24)
    SESSIONS[token] = uname
    return {
        "token": token,
        "username": uname,
        "balance": row["balance"],
        "is_admin": (uname == ADMIN_USERNAME.lower()),
        "price_per_survey": PRICE_PER_SURVEY,
    }


@app.get("/api/me")
def api_me(username: str = Depends(get_current_user)):
    with DB_LOCK:
        conn = get_db()
        user_row = conn.execute("SELECT username, balance FROM users WHERE username = ?", (username,)).fetchone()
        tx_rows = conn.execute(
            "SELECT amount, tx_type, note, created_at FROM transactions WHERE username = ? ORDER BY id DESC LIMIT 20",
            (username,),
        ).fetchall()
        conn.close()

    if not user_row:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng.")

    return {
        "username": user_row["username"],
        "balance": user_row["balance"],
        "is_admin": (username == ADMIN_USERNAME.lower()),
        "price_per_survey": PRICE_PER_SURVEY,
        "transactions": [dict(r) for r in tx_rows],
    }


# ==============================================================================
# API QUẢN TRỊ ADMIN
# ==============================================================================
@app.get("/api/admin/users")
def api_admin_users(admin_username: str = Depends(get_admin_user)):
    with DB_LOCK:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, username, balance, created_at FROM users ORDER BY id DESC"
        ).fetchall()
        conn.close()
    return {"users": [dict(r) for r in rows]}


@app.post("/api/admin/topup")
def api_admin_topup(req: AdminTopupRequest, admin_username: str = Depends(get_admin_user)):
    uname = req.target_username.strip().lower()
    if req.amount == 0:
        raise HTTPException(status_code=400, detail="Số tiền phải khác 0.")
    try:
        new_bal = adjust_balance(uname, req.amount, "TOPUP", req.note)
        return {"username": uname, "new_balance": new_bal}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==============================================================================
# XỬ LÝ QUÉT GOOGLE FORM & CHẠY KHẢO SÁT
# ==============================================================================
def resolve_urls(input_url: str):
    clean_input = input_url.strip()
    if not clean_input.startswith("http"):
        clean_input = "https://" + clean_input

    resp = requests.get(
        clean_input,
        headers={"User-Agent": random.choice(USER_AGENTS)},
        timeout=15,
    )
    resp.raise_for_status()

    real_view_url = resp.url.split("?")[0].rstrip("/")
    if real_view_url.endswith("/edit"):
        real_view_url = real_view_url[:-5] + "/viewform"
    elif not real_view_url.endswith("/viewform"):
        real_view_url = real_view_url + "/viewform"

    submit_url = real_view_url[:-9] + "/formResponse"
    return resp.text, real_view_url, submit_url


def parse_form_html(html_text: str):
    match = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(.*?);\s*</script>", html_text, re.S)
    if not match:
        raise ValueError("Không đọc được dữ liệu biểu mẫu. Hãy kiểm tra quyền truy cập công khai của Form.")

    raw_data = json.loads(match.group(1))
    form_title = raw_data[1][8] if (len(raw_data[1]) > 8 and raw_data[1][8]) else "Khảo sát Google Form"
    items = raw_data[1][1]

    questions = []
    page_count = 1

    for item in items:
        if not item or len(item) < 4:
            continue
        q_title = str(item[1]).strip()
        q_type = item[3]

        if q_type == 8:
            page_count += 1
            continue

        if len(item) < 5 or not item[4]:
            continue

        if q_type == 7 or len(item[4]) > 1:
            rows = []
            for sub in item[4]:
                entry_id = f"entry.{sub[0]}"
                row_label = sub[3][0] if (len(sub) > 3 and sub[3]) else "Dòng tiêu chí"
                options = [
                    str(opt[0])
                    for opt in (sub[1] or [])
                    if opt and opt[0] is not None and str(opt[0]).strip() != ""
                ]
                n = len(options)
                default_w = [round(100 // n) for _ in range(n)] if n else []
                if default_w:
                    default_w[0] += 100 - sum(default_w)
                rows.append({
                    "entry_id": entry_id,
                    "row_label": row_label,
                    "options": options,
                    "weights": default_w,
                })
            questions.append({"title": q_title, "kind": "grid", "rows": rows})

        elif q_type == 4:
            sub = item[4][0]
            entry_id = f"entry.{sub[0]}"
            options = [
                str(opt[0])
                for opt in (sub[1] or [])
                if opt and opt[0] is not None and str(opt[0]).strip() != ""
            ]
            questions.append({
                "title": q_title,
                "kind": "checkbox",
                "entry_id": entry_id,
                "options": options,
                "weights": [50 for _ in options],
            })

        elif q_type in (0, 1):
            sub = item[4][0]
            entry_id = f"entry.{sub[0]}"
            questions.append({
                "title": q_title,
                "kind": "text",
                "entry_id": entry_id,
                "samples": DEFAULT_TEXT_SAMPLES,
            })

        else:
            sub = item[4][0]
            entry_id = f"entry.{sub[0]}"
            options = [
                str(opt[0])
                for opt in (sub[1] or [])
                if opt and opt[0] is not None and str(opt[0]).strip() != ""
            ]
            n = len(options)
            default_w = [round(100 // n) for _ in range(n)] if n else []
            if default_w:
                default_w[0] += 100 - sum(default_w)
            questions.append({
                "title": q_title,
                "kind": "single",
                "entry_id": entry_id,
                "options": options,
                "weights": default_w,
            })

    page_history = ",".join(str(i) for i in range(page_count))
    return form_title, questions, page_history


def build_single_pool(options: List[str], weights: List[float], total_n: int) -> List[str]:
    if not options:
        return [""] * total_n
    w = [max(0.0, float(x)) for x in weights[: len(options)]]
    while len(w) < len(options):
        w.append(0.0)
    total_w = sum(w)
    if total_w <= 0:
        w = [1.0] * len(options)
        total_w = float(len(options))

    counts = [int(round((val / total_w) * total_n)) for val in w]
    diff = total_n - sum(counts)
    max_idx = w.index(max(w))
    counts[max_idx] = max(0, counts[max_idx] + diff)

    pool = []
    for opt, cnt in zip(options, counts):
        pool.extend([opt] * cnt)
    random.shuffle(pool)
    return pool


def build_checkbox_pool(options: List[str], percentages: List[float], total_n: int) -> List[List[str]]:
    if not options:
        return [[] for _ in range(total_n)]
    pcts = [max(0.0, min(100.0, float(x))) for x in percentages[: len(options)]]
    while len(pcts) < len(options):
        pcts.append(0.0)

    matrix = [[] for _ in range(total_n)]
    for opt, pct in zip(options, pcts):
        cnt = int(round((pct / 100.0) * total_n))
        cnt = max(0, min(total_n, cnt))
        for idx in random.sample(range(total_n), cnt):
            matrix[idx].append(opt)

    fallback_opt = options[pcts.index(max(pcts))] if max(pcts) > 0 else options[0]
    for row in matrix:
        if not row:
            row.append(fallback_opt)

    random.shuffle(matrix)
    return matrix


def execute_campaign_worker(task_id: str, username: str, req: RunRequest):
    task = TASKS[task_id]
    total = req.total
    payloads = [[("pageHistory", req.page_history)] for _ in range(total)]

    for q in req.questions:
        kind = q.get("kind")
        if kind == "grid":
            for row in q.get("rows", []):
                pool = build_single_pool(row["options"], row["weights"], total)
                for i in range(total):
                    payloads[i].append((row["entry_id"], pool[i]))
        elif kind == "checkbox":
            pool = build_checkbox_pool(q["options"], q["weights"], total)
            for i in range(total):
                for val in pool[i]:
                    payloads[i].append((q["entry_id"], val))
        elif kind == "text":
            samples = [s.strip() for s in q.get("samples", DEFAULT_TEXT_SAMPLES) if s.strip()]
            if not samples:
                samples = DEFAULT_TEXT_SAMPLES
            for i in range(total):
                payloads[i].append((q["entry_id"], random.choice(samples)))
        else:
            pool = build_single_pool(q["options"], q["weights"], total)
            for i in range(total):
                payloads[i].append((q["entry_id"], pool[i]))

    for i, data_items in enumerate(payloads, start=1):
        if task["stop_requested"]:
            task["status"] = "stopped"
            task["logs"].append(f"[DỪNG] Đã dừng tại phiếu {i - 1}/{total}.")
            break

        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": req.view_url,
            "Origin": "https://docs.google.com",
        }
        try:
            res = requests.post(req.submit_url, data=data_items, headers=headers, timeout=15)
            if res.status_code == 200:
                task["success"] += 1
                task["logs"].append(f"[{i}/{total}] Thành công (HTTP 200)")
            else:
                task["failed"] += 1
                task["logs"].append(f"[{i}/{total}] Thất bại (HTTP {res.status_code})")
        except Exception as exc:
            task["failed"] += 1
            task["logs"].append(f"[{i}/{total}] Lỗi mạng: {exc}")

        task["sent"] = i
        if len(task["logs"]) > 100:
            task["logs"] = task["logs"][-100:]

        if i < total and not task["stop_requested"]:
            sleep_sec = random.uniform(min(req.delay_min, req.delay_max), max(req.delay_min, req.delay_max))
            time.sleep(max(0.2, sleep_sec))

    unfulfilled = total - task["success"]
    if unfulfilled > 0:
        refund_amount = unfulfilled * PRICE_PER_SURVEY
        adjust_balance(
            username,
            refund_amount,
            "REFUND",
            f"Hoàn tiền {unfulfilled} phiếu chưa hoàn thành (Mã phiên {task_id})",
        )
        task["logs"].append(f"[HOÀN TIỀN] Đã hoàn {refund_amount:,}đ cho {unfulfilled} phiếu chưa thành công.")

    if task["status"] == "running":
        task["status"] = "completed"
        task["logs"].append(f"[HOÀN TẤT] Thành công {task['success']}/{total} phiếu.")


@app.get("/")
def serve_homepage():
    return FileResponse("index.html")


@app.post("/api/scan")
def api_scan_form(req: ScanRequest, username: str = Depends(get_current_user)):
    try:
        html_text, real_view_url, submit_url = resolve_urls(req.url)
        form_title, questions, page_history = parse_form_html(html_text)
        return {
            "form_title": form_title,
            "view_url": real_view_url,
            "submit_url": submit_url,
            "page_history": page_history,
            "questions": questions,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/run")
def api_run_campaign(req: RunRequest, username: str = Depends(get_current_user)):
    if req.total < 1 or req.total > 2000:
        raise HTTPException(status_code=400, detail="Số lượng phiếu phải nằm trong khoảng 1 đến 2000.")

    total_cost = req.total * PRICE_PER_SURVEY
    task_id = str(uuid.uuid4())[:8]

    try:
        new_balance = adjust_balance(
            username,
            -total_cost,
            "CHARGE",
            f"Chạy {req.total} phiếu khảo sát (Mã phiên {task_id})",
        )
    except ValueError:
        raise HTTPException(
            status_code=402,
            detail=f"Số dư không đủ! Cần {total_cost:,}đ cho {req.total} phiếu (250đ/phiếu). Vui lòng nạp thêm tiền.",
        )

    TASKS[task_id] = {
        "task_id": task_id,
        "owner": username,
        "status": "running",
        "total": req.total,
        "sent": 0,
        "success": 0,
        "failed": 0,
        "stop_requested": False,
        "logs": [f"[*] Đã trừ {total_cost:,}đ. Bắt đầu chạy {req.total} phiếu khảo sát..."],
    }

    worker = threading.Thread(target=execute_campaign_worker, args=(task_id, username, req), daemon=True)
    worker.start()
    return {"task_id": task_id, "new_balance": new_balance}


@app.post("/api/stop/{task_id}")
def api_stop_campaign(task_id: str, username: str = Depends(get_current_user)):
    if task_id not in TASKS or TASKS[task_id]["owner"] != username:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiên chạy.")
    TASKS[task_id]["stop_requested"] = True
    return {"ok": True}


@app.get("/api/status/{task_id}")
def api_get_status(task_id: str, username: str = Depends(get_current_user)):
    if task_id not in TASKS or TASKS[task_id]["owner"] != username:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiên chạy.")
    return TASKS[task_id]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)