# -*- coding: utf-8 -*-
"""
واجهة ويب محلية لإدارة قائمة دبلجة الفيديوهات.
تشتغل على جوالك عبر ترمكس، وتفتحها من متصفح الجوال على:
    http://127.0.0.1:8890

الحالة (قائمة المهام وتقدّمها) تُحفظ بملف JSON على القرص، فتحديث الصفحة أو
حتى إغلاق المتصفح وفتحه بعد ساعات ما يفقد أي معلومة — لأن مصدر الحقيقة
هو السيرفر، مو المتصفح.
"""

import os
import sys
import json
import time
import uuid
import threading
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from pipeline import run_pipeline, PipelineError  # noqa: E402

STATE_DIR = os.path.expanduser("~/dub_local")
STATE_FILE = os.path.join(STATE_DIR, "webui_state.json")
MAX_QUEUE = 5

os.makedirs(STATE_DIR, exist_ok=True)

app = Flask(__name__)
lock = threading.RLock()
tasks = []
worker_running = False
cancel_flags = {}  # task_id -> bool


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def load_state():
    global tasks
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for t in loaded:
                # أي مهمة كانت "شغالة" وقت ما توقف السيرفر تُعتبر منقطعة،
                # لأننا ما نقدر نستأنف عملية yt-dlp/ffmpeg بمنتصفها بأمان
                if t.get("status") not in ("done", "error", "queued"):
                    t["status"] = "error"
                    t["message"] = "⚠️ انقطعت المعالجة (تم إيقاف السيرفر). احذف المهمة وأضفها من جديد."
            tasks = loaded
        except Exception:
            tasks = []


def update_task(task_id, **fields):
    with lock:
        for t in tasks:
            if t["id"] == task_id:
                t.update(fields)
                t["updated_at"] = datetime.now().isoformat()
                break
        save_state()


def worker_loop():
    global worker_running
    while True:
        with lock:
            next_task = next((t for t in tasks if t["status"] == "queued"), None)
            if not next_task:
                worker_running = False
                return
            task_id = next_task["id"]
            url = next_task["url"]
            whisper_model = next_task["whisper_model"]

        update_task(task_id, status="downloading_video", progress=1, message="جاري البدء...")

        def progress_cb(**fields):
            update_task(task_id, **fields)

        def cancel_check():
            return cancel_flags.get(task_id, False)

        try:
            final_path = run_pipeline(url, whisper_model, progress_cb, cancel_check)
            update_task(task_id, status="done", progress=100, message="تم بنجاح ✅",
                        final_path=final_path)
        except PipelineError as e:
            update_task(task_id, status="error", message=str(e))
        except Exception as e:
            update_task(task_id, status="error", message=f"خطأ غير متوقع: {e}")
        finally:
            cancel_flags.pop(task_id, None)


def ensure_worker():
    global worker_running
    with lock:
        if worker_running:
            return
        worker_running = True
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    with lock:
        return jsonify(tasks)


@app.route("/api/tasks", methods=["POST"])
def add_task():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    whisper_model = data.get("whisper_model", "small")
    if whisper_model not in ("tiny", "base", "small", "medium"):
        whisper_model = "small"
    if not url:
        return jsonify({"error": "الرابط فارغ"}), 400
    if "youtube.com" not in url and "youtu.be" not in url:
        return jsonify({"error": "الرابط لازم يكون رابط يوتيوب صحيح"}), 400

    with lock:
        active = [t for t in tasks if t["status"] not in ("done", "error")]
        if len(active) >= MAX_QUEUE:
            return jsonify({"error": f"الحد الأقصى {MAX_QUEUE} مهام بنفس الوقت بقائمة الانتظار"}), 400

        task = {
            "id": uuid.uuid4().hex[:8],
            "url": url,
            "whisper_model": whisper_model,
            "status": "queued",
            "progress": 0,
            "message": "بانتظار الدور...",
            "video_size": None,
            "final_path": None,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        tasks.append(task)
        save_state()

    ensure_worker()
    return jsonify(task)


@app.route("/api/tasks/<task_id>", methods=["DELETE"])
def delete_task(task_id):
    with lock:
        t = next((t for t in tasks if t["id"] == task_id), None)
        if not t:
            return jsonify({"error": "المهمة غير موجودة"}), 404
        if t["status"] in ("queued", "done", "error"):
            tasks.remove(t)
            save_state()
            return jsonify({"ok": True})
        # مهمة شغالة حاليًا -> اطلب إلغاء بدل الحذف المباشر
        cancel_flags[task_id] = True
        return jsonify({"ok": True, "cancelling": True})


if __name__ == "__main__":
    load_state()
    ensure_worker()  # يكمل أي مهام كانت "queued" ولسه ما بدأت
    port = int(os.environ.get("PORT", 8890))
    print(f"🌐 افتح المتصفح على: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, threaded=True)
