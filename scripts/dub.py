import os
import sys
import time
import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from deep_translator import GoogleTranslator
import edge_tts
from pydub import AudioSegment
from faster_whisper import WhisperModel

WORK_DIR = "work"
os.makedirs(WORK_DIR, exist_ok=True)

# يمكن التحكم فيه من الـ workflow عبر env var WHISPER_MODEL
# القيم الممكنة: tiny, base, small, medium, large-v3
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")

# صوت Edge TTS العربي (بديل gTTS، جودة طبيعية أعلى بكثير)
# أصوات عربية متاحة (أمثلة): ar-SA-HamedNeural (رجالي)، ar-SA-ZariyahNeural (نسائي)،
# ar-EG-ShakirNeural (رجالي مصري)، ar-EG-SallyNeural (نسائي مصري)
EDGE_VOICE = os.environ.get("EDGE_VOICE", "ar-SA-HamedNeural")

AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".ogg", ".opus", ".aac")


def retry(fn, tries=3, delay=2, what="العملية"):
    """يعيد محاولة تنفيذ fn عدة مرات قبل ما يفشل نهائيًا."""
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            last_err = e
            print(f"⚠️  فشلت {what} (محاولة {attempt}/{tries}): {e}")
            if attempt < tries:
                time.sleep(delay)
    raise last_err


def find_source_audio():
    """يدور على ملف الصوت المصدري اللي نزّله الـ workflow جوه work/."""
    if not os.path.isdir(WORK_DIR):
        return None
    for name in sorted(os.listdir(WORK_DIR)):
        if name.lower().endswith(AUDIO_EXTENSIONS):
            return os.path.join(WORK_DIR, name)
    return None


def transcribe(audio_path):
    print(f"📝 جاري تفريغ الكلام (faster-whisper / {WHISPER_MODEL_SIZE} / CPU)...")
    model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    segments_iter, info = model.transcribe(
        audio_path,
        beam_size=5,
        vad_filter=True,
        # يمنع الموديل من "يهلوس" بتكرار كلام وهمي (غالبًا أرقام أو عبارات
        # متكررة) بالاعتماد على نص المقطع السابق كسياق — سبب شائع جدًا
        # لظهور صوت يقرأ أرقام عشوائية فوق الدبلجة الصحيحة
        condition_on_previous_text=False,
    )
    print(f"   اللغة المكتشفة: {info.language} (ثقة {info.language_probability:.2f})")

    segments = []
    skipped_hallucinations = 0
    for seg in segments_iter:
        text = seg.text.strip()
        if not text:
            continue

        # فلترة المقاطع المشكوك فيها (هلاوس) بناءً على مؤشرات ثقة faster-whisper:
        # - no_speech_prob عالي: احتمال كبير إنه مو كلام أصلًا (موسيقى/ضجيج)
        # - avg_logprob منخفض جدًا: الموديل نفسه غير واثق من النص اللي طلعه
        # - compression_ratio عالي: بصمة كلاسيكية لنص متكرر/عشوائي (هلوسة)
        is_suspicious = (
            seg.no_speech_prob > 0.6
            or seg.avg_logprob < -1.0
            or seg.compression_ratio > 2.4
        )
        if is_suspicious:
            skipped_hallucinations += 1
            continue

        segments.append({"start": seg.start * 1000, "end": seg.end * 1000, "text": text})

    if skipped_hallucinations:
        print(f"   ⏭️ تم تجاهل {skipped_hallucinations} مقطع مشكوك فيه (هلوسة محتملة)")
    print(f"✅ تم استخراج {len(segments)} جملة")
    return segments


def translate_segments(segments):
    """يترجم كل الجمل بالتوازي (طلبات شبكة مستقلة)، بدل ما ينتظر كل وحدة
    لحالها. Google Translate غير الرسمي حساس للحظر لو تزامن عالي جدًا،
    فخليناه معتدل (6 بشكل افتراضي) بدل ١٠ زي TTS."""
    concurrency = int(os.environ.get("TRANSLATE_CONCURRENCY", "6"))
    print(f"🌐 جاري ترجمة {len(segments)} جملة للعربية (تزامن×{concurrency})...")

    def _translate_one(i, seg):
        def _tr():
            return GoogleTranslator(source="auto", target="ar").translate(seg["text"])
        try:
            return i, retry(_tr, tries=3, delay=2, what=f"ترجمة الجملة {i+1}")
        except Exception:
            return i, seg["text"]  # نرجع للنص الأصلي لو فشلت الترجمة نهائيًا

    results = {}
    done_count = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_translate_one, i, seg) for i, seg in enumerate(segments)]
        for future in as_completed(futures):
            i, ar_text = future.result()
            results[i] = ar_text
            done_count += 1
            if done_count % 20 == 0:
                print(f"   ترجمة: {done_count}/{len(segments)}")

    for i, seg in enumerate(segments):
        seg["ar_text"] = results.get(i, seg["text"])

    print("✅ تمت الترجمة")


def get_duration_ms(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return int(float(out.stdout.strip()) * 1000)


# عدد طلبات Edge TTS المتزامنة (بالتوازي). رقم أعلى = أسرع، لكن لو ارتفع
# كثير قد تُرفض بعض الطلبات من خدمة مايكروسوفت — نعيد محاولتها تلقائيًا.
TTS_CONCURRENCY = int(os.environ.get("TTS_CONCURRENCY", "10"))


async def _synthesize_one(sem, text, out_path, rate_percent, tries=3):
    """يولّد مقطع صوت وحد، مع إعادة محاولة عند الفشل، تحت حد التزامن sem."""
    sign = "+" if rate_percent >= 0 else ""
    rate_str = f"{sign}{rate_percent}%"
    async with sem:
        for attempt in range(1, tries + 1):
            try:
                communicate = edge_tts.Communicate(text, EDGE_VOICE, rate=rate_str)
                await communicate.save(out_path)
                return True
            except Exception as e:
                if attempt == tries:
                    print(f"   ⚠️ فشل توليد صوت مقطع ({os.path.basename(out_path)}): {e}")
                    return False
                await asyncio.sleep(1.5)
    return False


def synthesize_batch(jobs):
    """jobs: قائمة (مسار_الملف, النص, نسبة_السرعة). ينفذها كلها بالتوازي
    بحد أقصى TTS_CONCURRENCY طلب بنفس الوقت، بدل ما ينتظر كل واحد لحاله."""
    async def _run_all():
        sem = asyncio.Semaphore(TTS_CONCURRENCY)
        tasks = [_synthesize_one(sem, text, path, rate) for path, text, rate in jobs]
        return await asyncio.gather(*tasks)

    return asyncio.run(_run_all())


def build_dubbed_audio(segments, total_duration_ms, output_audio_path):
    print(f"🔊 جاري توليد الصوت العربي (Edge TTS / {EDGE_VOICE}، تزامن×{TTS_CONCURRENCY})...")

    valid = [(i, seg) for i, seg in enumerate(segments) if seg["ar_text"].strip()]
    paths = {i: os.path.join(WORK_DIR, f"seg_{i}.mp3") for i, _ in valid}

    # الدفعة الأولى: توليد كل الجمل بالتوازي بسرعة كلام طبيعية (0%)
    print(f"   توليد {len(valid)} مقطع صوتي بالتوازي...")
    jobs = [(paths[i], seg["ar_text"].strip(), 0) for i, seg in valid]
    synthesize_batch(jobs)

    # الدفعة الثانية: الجمل اللي مدتها بعيدة عن مدة الجملة الأصلية تُعاد
    # بسرعة كلام معدّلة — أيضًا بالتوازي، مو وحدة وحدة
    retry_jobs = []
    for i, seg in valid:
        p = paths[i]
        if not os.path.exists(p):
            continue
        clip = AudioSegment.from_mp3(p)
        target_duration = seg["end"] - seg["start"]
        if len(clip) > 0 and target_duration > 0:
            ratio = len(clip) / target_duration
            if abs(ratio - 1.0) > 0.08:
                rate_percent = int(max(-40, min(60, (ratio - 1.0) * 100)))
                retry_jobs.append((p, seg["ar_text"].strip(), rate_percent))

    if retry_jobs:
        print(f"   ضبط سرعة {len(retry_jobs)} مقطع لمطابقة التوقيت (بالتوازي)...")
        synthesize_batch(retry_jobs)

    # التركيب النهائي: عملية محلية سريعة (بدون شبكة)، تصير بالترتيب
    final_audio = AudioSegment.silent(duration=total_duration_ms)
    for i, seg in valid:
        p = paths[i]
        if not os.path.exists(p):
            continue
        clip = AudioSegment.from_mp3(p)

        start_pos = int(seg["start"])
        max_len = total_duration_ms - start_pos
        if max_len <= 0:
            continue
        if len(clip) > max_len:
            clip = clip[:max_len]

        final_audio = final_audio.overlay(clip, position=start_pos)

    # ضمان أن مدة الصوت النهائي تطابق مدة الصوت الأصلي تمامًا
    if len(final_audio) > total_duration_ms:
        final_audio = final_audio[:total_duration_ms]
    elif len(final_audio) < total_duration_ms:
        final_audio = final_audio + AudioSegment.silent(duration=total_duration_ms - len(final_audio))

    final_audio.export(output_audio_path, format="mp3")
    print("✅ تم توليد الصوت المدبلج")


def main():
    t_start = time.time()

    audio_path = find_source_audio()
    if not audio_path:
        print("❌ ما فيه ملف صوت مصدري بمجلد work/ (لازم الـ workflow ينزّله من الـ Release أول)")
        sys.exit(1)

    print(f"🎧 ملف الصوت المصدري: {audio_path}")

    total_duration = get_duration_ms(audio_path)
    os.makedirs("output", exist_ok=True)
    dubbed_audio_path = os.path.join("output", "dubbed_audio.mp3")

    t1 = time.time()
    segments = transcribe(audio_path)
    t2 = time.time()
    print(f"⏱️ مدة التفريغ (Whisper): {t2 - t1:.0f} ثانية")

    if not segments:
        print("⚠️ ما فيه كلام واضح بالصوت (يمكن صامت أو موسيقى فقط) — تخطي الدبلجة")
        AudioSegment.silent(duration=total_duration).export(dubbed_audio_path, format="mp3")
        print(f"🎉 خلص (بدون دبلجة)! ملف الصوت بمسار: {dubbed_audio_path}")
        return

    translate_segments(segments)
    t3 = time.time()
    print(f"⏱️ مدة الترجمة: {t3 - t2:.0f} ثانية")

    build_dubbed_audio(segments, total_duration, dubbed_audio_path)
    t4 = time.time()
    print(f"⏱️ مدة توليد الصوت (TTS): {t4 - t3:.0f} ثانية")

    print(f"🎉 خلص! ملف الصوت المدبلج بمسار: {dubbed_audio_path}")
    print(f"⏱️ الوقت الكلي داخل GitHub Actions: {t4 - t_start:.0f} ثانية "
          f"({(t4 - t_start) / 60:.1f} دقيقة)")


if __name__ == "__main__":
    main()
