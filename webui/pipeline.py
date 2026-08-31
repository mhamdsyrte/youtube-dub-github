# -*- coding: utf-8 -*-
"""
منطق تنفيذ عملية الدبلجة الكاملة (نسخة بايثون من local-tools/dub_from_phone.sh)
مع تقارير تقدم حيّة تُرسل عبر progress_cb لكل خطوة، عشان تنعرض بواجهة الويب.

لا يمسّ هذا الملف "المحرك" الحقيقي (faster-whisper / الترجمة / Edge TTS اللي
تشتغل داخل GitHub Actions بملف scripts/dub.py) — فقط ينسّق نفس خطوات التحميل
المحلي / الرفع / المراقبة / الدمج اللي كانت بالسكربت الأصلي.
"""

import os
import re
import json
import time
import shutil
import subprocess
from datetime import datetime, timezone


class PipelineError(Exception):
    pass


def human_size(num_bytes):
    if not num_bytes:
        return None
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def has_audio_stream(video_path):
    """يتحقق هل الفيديو فيه أي مسار صوت أصلًا، قبل ما نحاول نفصله."""
    out = _run([
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", video_path,
    ])
    return out.returncode == 0 and out.stdout.strip() != ""


def get_repo():
    out = _run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    if out.returncode != 0:
        raise PipelineError(f"تعذّر تحديد المستودع (تأكد من gh auth login): {out.stderr.strip()}")
    return out.stdout.strip()


# نسبة تقدم كل مرحلة (تجميعية) داخل الشريط الكلي
STAGE_RANGE = {
    "downloading_video": (2, 35),
    "extracting_audio": (35, 45),
    "uploading_audio": (45, 55),
    "running_on_github": (55, 88),
    "downloading_dubbed_audio": (88, 95),
    "merging": (95, 99),
}


def run_pipeline(url, whisper_model, progress_cb, cancel_check=None):
    """
    ينفّذ خط الأنابيب الكامل لفيديو واحد ويرجع مسار الملف النهائي.
    progress_cb(status=None, progress=None, message=None, **extra) يُستدعى
    باستمرار لتحديث حالة المهمة.
    cancel_check() دالة اختيارية ترجع True لو المستخدم طلب إلغاء المهمة.
    """
    repo = get_repo()
    tag = f"audio-{int(time.time())}"
    work = os.path.expanduser(f"~/dub_local/{tag}")
    os.makedirs(work, exist_ok=True)

    video_file = os.path.join(work, "source_video.mp4")
    audio_file = os.path.join(work, "source_audio.mp3")
    dubbed_audio_file = os.path.join(work, "dubbed_audio.mp3")
    output_dir = os.path.expanduser("~/storage/downloads")
    os.makedirs(output_dir, exist_ok=True)
    final_file = os.path.join(
        output_dir, f"dubbed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    )

    def check_cancel():
        if cancel_check and cancel_check():
            raise PipelineError("تم إلغاء المهمة بواسطة المستخدم")

    # ---------- 1) تحميل الفيديو محليًا (مع نسبة تقدم حقيقية من yt-dlp) ----------
    lo, hi = STAGE_RANGE["downloading_video"]
    progress_cb(status="downloading_video", progress=lo, message="جاري تحميل الفيديو...")

    cmd = [
        "yt-dlp", "-f",
        "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4", "--newline", "-o", video_file, url,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    pct_re = re.compile(r"([\d.]+)%\s+of\s+~?([\d.]+)(K|M|G)iB")
    video_size_human = None
    for line in proc.stdout:
        check_cancel()
        m = pct_re.search(line)
        if m:
            pct = float(m.group(1))
            size_num, unit = float(m.group(2)), m.group(3)
            mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[unit]
            video_size_human = human_size(size_num * mult)
            mapped = lo + (pct / 100.0) * (hi - lo)
            progress_cb(progress=round(mapped, 1), video_size=video_size_human,
                        message=f"تحميل الفيديو... {pct:.0f}%")
    proc.wait()
    if proc.returncode != 0 or not os.path.exists(video_file):
        raise PipelineError("فشل تحميل الفيديو من يوتيوب (تأكد من الرابط)")

    actual_size = os.path.getsize(video_file)
    video_size_human = human_size(actual_size)

    # ---------- تخطّي الفيديوهات اللي ما فيها صوت أصلًا (بدون فشل المهمة) ----------
    check_cancel()
    if not has_audio_stream(video_file):
        progress_cb(video_size=video_size_human,
                    message="⏭️ الفيديو بدون صوت، تم تخطي الدبلجة")
        shutil.copy2(video_file, final_file)
        shutil.rmtree(work, ignore_errors=True)
        progress_cb(progress=100, message="⏭️ لا يوجد صوت بالفيديو — تم حفظه كما هو بدون دبلجة")
        return final_file

    # ---------- 2) فصل الصوت ----------
    check_cancel()
    progress_cb(status="extracting_audio", progress=STAGE_RANGE["extracting_audio"][0],
                video_size=video_size_human, message="جاري فصل الصوت من الفيديو...")
    r = _run(["ffmpeg", "-y", "-i", video_file, "-vn", "-ar", "16000", "-ac", "1",
              "-b:a", "64k", audio_file, "-loglevel", "error"])
    if r.returncode != 0 or not os.path.exists(audio_file):
        raise PipelineError(f"فشل فصل الصوت: {r.stderr.strip()[:300]}")

    # ---------- 3) رفع الصوت فقط كإصدار مؤقت ----------
    check_cancel()
    progress_cb(status="uploading_audio", progress=STAGE_RANGE["uploading_audio"][0],
                message="جاري رفع الصوت لـ GitHub...")
    r = _run(["gh", "release", "create", tag, audio_file, "--repo", repo,
              "--title", f"Temp audio {tag}",
              "--notes", "رفع مؤقت لمعالجة الدبلجة، يُحذف تلقائيًا بعد الانتهاء"])
    if r.returncode != 0:
        raise PipelineError(f"فشل رفع الصوت: {r.stderr.strip()[:300]}")

    # ---------- 4) تشغيل الـ workflow ----------
    check_cancel()
    progress_cb(status="running_on_github", progress=STAGE_RANGE["running_on_github"][0],
                message="جاري تشغيل الدبلجة على GitHub Actions...")
    trigger_time = time.time()
    r = _run(["gh", "workflow", "run", "dub.yml", "--repo", repo,
              "-f", f"audio_release_tag={tag}", "-f", f"whisper_model={whisper_model}"])
    if r.returncode != 0:
        raise PipelineError(f"فشل تشغيل الـ workflow: {r.stderr.strip()[:300]}")

    # ابحث عن الـ run الجديد
    run_id = None
    for _ in range(25):
        check_cancel()
        time.sleep(2)
        out = _run(["gh", "run", "list", "--repo", repo, "--workflow=dub.yml",
                     "--limit", "5", "--json", "databaseId,createdAt,status"])
        try:
            runs = json.loads(out.stdout)
        except Exception:
            runs = []
        for rrun in runs:
            created = datetime.fromisoformat(rrun["createdAt"].replace("Z", "+00:00")).timestamp()
            if created >= trigger_time - 20:
                run_id = rrun["databaseId"]
                break
        if run_id:
            break
    if not run_id:
        raise PipelineError("تعذّر العثور على تشغيلة الـ workflow الجديدة على GitHub")

    # ---------- 5) متابعة تقدم الـ workflow (عبر عدد الخطوات المكتملة) ----------
    lo, hi = STAGE_RANGE["running_on_github"]
    total_steps_estimate = 7
    while True:
        check_cancel()
        time.sleep(5)
        out = _run(["gh", "run", "view", str(run_id), "--repo", repo,
                     "--json", "status,conclusion,jobs"])
        try:
            info = json.loads(out.stdout)
        except Exception:
            continue
        status = info.get("status")
        jobs = info.get("jobs", [])
        completed_steps = sum(
            1 for job in jobs for step in job.get("steps", [])
            if step.get("status") == "completed"
        )
        frac = min(completed_steps / total_steps_estimate, 0.97)
        mapped = lo + frac * (hi - lo)
        progress_cb(progress=round(mapped, 1), message="جاري المعالجة على GitHub Actions...")
        if status == "completed":
            conclusion = info.get("conclusion")
            if conclusion != "success":
                raise PipelineError(f"فشلت تشغيلة GitHub Actions (النتيجة: {conclusion})")
            break

    # ---------- 6) تنزيل الصوت المدبلج ----------
    check_cancel()
    progress_cb(status="downloading_dubbed_audio", progress=STAGE_RANGE["downloading_dubbed_audio"][0],
                message="جاري تنزيل الصوت المدبلج...")
    r = _run(["gh", "run", "download", str(run_id), "--repo", repo, "-n", "dubbed-audio", "-D", work])
    if r.returncode != 0 or not os.path.exists(dubbed_audio_file):
        raise PipelineError("لم يتم العثور على ملف الصوت المدبلج بعد التنزيل")

    # ---------- 7) الدمج النهائي محليًا ----------
    check_cancel()
    progress_cb(status="merging", progress=STAGE_RANGE["merging"][0],
                message="جاري دمج الصوت مع الفيديو...")
    r = _run(["ffmpeg", "-y", "-i", video_file, "-i", dubbed_audio_file,
              "-c:v", "copy", "-map", "0:v:0", "-map", "1:a:0", "-shortest",
              final_file, "-loglevel", "error"])
    if r.returncode != 0 or not os.path.exists(final_file):
        raise PipelineError(f"فشل دمج الفيديو النهائي: {r.stderr.strip()[:300]}")

    # ---------- 8) تنظيف ----------
    _run(["gh", "release", "delete", tag, "--yes", "--repo", repo])
    shutil.rmtree(work, ignore_errors=True)

    progress_cb(progress=100, message="تم بنجاح ✅")
    return final_file
