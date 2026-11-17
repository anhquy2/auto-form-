import json
import os
import random
import re
import threading
import time
import uuid
from typing import Any, Dict, List

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="Auto Google Form Web Tool")

# Kho lưu trữ trạng thái các phiên chạy ngầm trong bộ nhớ
TASKS: Dict[str, Dict[str, Any]] = {}

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


def resolve_urls(input_url: str):
    """Giải mã link rút gọn forms.gle hoặc link /edit sang link gốc viewform và formResponse."""
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
    """Bóc tách toàn bộ câu hỏi, loại câu hỏi và danh sách đáp án từ FB_PUBLIC_LOAD_DATA_."""
    match = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(.*?);\s*</script>", html_text, re.S)
    if not match:
        raise ValueError("Không đọc được dữ liệu biểu mẫu. Hãy kiểm tra lại quyền truy cập công khai của Form.")

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

        # Ngắt trang (Section break)
        if q_type == 8:
            page_count += 1
            continue

        if len(item) < 5 or not item[4]:
            continue

        # Nhóm câu hỏi Lưới (Grid) hoặc có nhiều dòng con
        if q_type == 7 or len(item[4]) > 1:
            rows = []
            for sub in item[4]:
                entry_id = f"entry.{sub[0]}"
                row_label = sub[3][0] if (len(sub) > 3 and sub[3]) else "Dòng tiêu chí"
                options = []
                if len(sub) > 1 and sub[1]:
                    for opt in sub[1]:
                        if opt and opt[0] is not None and str(opt[0]).strip() != "":
                            options.append(str(opt[0]))
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

            questions.append({
                "title": q_title,
                "kind": "grid",
                "rows": rows,
            })

        # Câu hỏi Checkbox (chọn nhiều đáp án)
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

        # Câu hỏi Tự luận ngắn / đoạn văn
        elif q_type in (0, 1):
            sub = item[4][0]
            entry_id = f"entry.{sub[0]}"
            questions.append({
                "title": q_title,
                "kind": "text",
                "entry_id": entry_id,
                "samples": DEFAULT_TEXT_SAMPLES,
            })

        # Câu hỏi Chọn 1 đáp án (Multiple choice, Dropdown, Linear scale)
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


def execute_campaign_worker(task_id: str, req: RunRequest):
    """Tiến trình chạy ngầm gửi từng phiếu khảo sát lên Google Forms."""
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
            task["logs"].append(f"[DỪNG] Người dùng đã dừng chiến dịch tại phiếu {i - 1}/{total}.")
            return

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
                task["logs"].append(f"[{i}/{total}] Thất bại (HTTP {res.status_code}) - Kiểm tra cài đặt Form")
        except Exception as exc:
            task["failed"] += 1
            task["logs"].append(f"[{i}/{total}] Lỗi mạng: {exc}")

        task["sent"] = i
        if len(task["logs"]) > 100:
            task["logs"] = task["logs"][-100:]

        if i < total and not task["stop_requested"]:
            sleep_sec = random.uniform(min(req.delay_min, req.delay_max), max(req.delay_min, req.delay_max))
            time.sleep(max(0.2, sleep_sec))

    task["status"] = "completed"
    task["logs"].append(f"[HOÀN TẤT] Thành công {task['success']}/{total} phiếu.")


@app.get("/")
def serve_homepage():
    return FileResponse("index.html")


@app.post("/api/scan")
def api_scan_form(req: ScanRequest):
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
def api_run_campaign(req: RunRequest):
    if req.total < 1 or req.total > 2000:
        raise HTTPException(status_code=400, detail="Số lượng phiếu phải nằm trong khoảng 1 đến 2000.")

    task_id = str(uuid.uuid4())[:8]
    TASKS[task_id] = {
        "task_id": task_id,
        "status": "running",
        "total": req.total,
        "sent": 0,
        "success": 0,
        "failed": 0,
        "stop_requested": False,
        "logs": [f"[*] Bắt đầu chạy {req.total} phiếu khảo sát..."],
    }

    worker = threading.Thread(target=execute_campaign_worker, args=(task_id, req), daemon=True)
    worker.start()
    return {"task_id": task_id}


@app.post("/api/stop/{task_id}")
def api_stop_campaign(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiên chạy.")
    TASKS[task_id]["stop_requested"] = True
    return {"ok": True}


@app.get("/api/status/{task_id}")
def api_get_status(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiên chạy.")
    return TASKS[task_id]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)