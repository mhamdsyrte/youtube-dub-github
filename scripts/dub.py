import os
import sys
import time
import subprocess
from deep_translator import GoogleTranslator
from gtts import gTTS
from pydub import AudioSegment
from faster_whisper import WhisperModel

WORK_DIR = "work"
os.makedirs(WORK_DIR, exist_ok=True)

# يمكن التحكم فيه من الـ workflow عبر env var WHISPER_MODEL
# القيم الممكنة: tiny, base, small, medium, large-v3
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")

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
    segments_iter, info = model.transcribe(audio_path, beam_size=5, vad_filter=True)
    print(f"   اللغة المكتشفة: {info.language} (ثقة {info.language_probability:.2f})")
    segments = []
    for seg in segments_iter:
        text = seg.text.strip()
        if text:
            segments.append({"start": seg.start * 1000, "end": seg.end * 1000, "text": text})
    print(f"✅ تم استخراج {len(segments)} جملة")
    return segments


def translate_segments(segments):
    print(f"🌐 جاري ترجمة {len(segments)} جملة للعربية...")
    translator = GoogleTranslator(source="auto", target="ar")

    for i, seg in enumerate(segments):
        def _tr():
            return translator.translate(seg["text"])

        try:
            seg["ar_text"] = retry(_tr, tries=3, delay=3, what=f"ترجمة الجملة {i+1}")
        except Exception:
            print(f"   ⚠️ تعذّرت ترجمة الجملة {i+1}، رح تبقى بلغتها الأصلية")
            seg["ar_text"] = seg["text"]

        # تهدئة بسيطة لتفادي حظر مؤقت من خدمة الترجمة المجانية
        time.sleep(0.15)

        if i % 20 == 0:
            print(f"   ترجمة الجملة {i+1}/{len(segments)}")
    print("✅ تمت الترجمة")


def get_duration_ms(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return int(float(out.stdout.strip()) * 1000)


def build_dubbed_audio(segments, total_duration_ms, output_audio_path):
    print("🔊 جاري توليد وتركيب الصوت العربي...")
    final_audio = AudioSegment.silent(duration=total_duration_ms)

    for i, seg in enumerate(segments):
        text = seg["ar_text"].strip()
        if not text:
            continue

        tmp_mp3 = os.path.join(WORK_DIR, f"seg_{i}.mp3")

        def _tts():
            gTTS(text=text, lang="ar").save(tmp_mp3)

        try:
            retry(_tts, tries=3, delay=2, what=f"توليد صوت الجملة {i+1}")
        except Exception:
            print(f"   ⚠️ تعذّر توليد صوت الجملة {i+1}، تم تخطيها")
            continue

        clip = AudioSegment.from_mp3(tmp_mp3)
        target_duration = seg["end"] - seg["start"]

        if len(clip) > 0 and target_duration > 0:
            speed_ratio = len(clip) / target_duration
            speed_ratio = max(0.75, min(speed_ratio, 1.6))
            if abs(speed_ratio - 1.0) > 0.05:
                sped_path = os.path.join(WORK_DIR, f"seg_{i}_speed.mp3")
                subprocess.run(
                    ["ffmpeg", "-y", "-i", tmp_mp3, "-filter:a", f"atempo={speed_ratio}", sped_path],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                clip = AudioSegment.from_mp3(sped_path)

        # لو المقطع يتجاوز مدة الصوت الكلية (آخر جملة أحيانًا)، نقصه بدل ما نمدد الصوت الكلي
        start_pos = int(seg["start"])
        max_len = total_duration_ms - start_pos
        if max_len <= 0:
            continue
        if len(clip) > max_len:
            clip = clip[:max_len]

        final_audio = final_audio.overlay(clip, position=start_pos)

        if i % 10 == 0:
            print(f"   تركيب الصوت: {i+1}/{len(segments)}")

    # ضمان أن مدة الصوت النهائي تطابق مدة الصوت الأصلي تمامًا
    if len(final_audio) > total_duration_ms:
        final_audio = final_audio[:total_duration_ms]
    elif len(final_audio) < total_duration_ms:
        final_audio = final_audio + AudioSegment.silent(duration=total_duration_ms - len(final_audio))

    final_audio.export(output_audio_path, format="mp3")
    print("✅ تم توليد الصوت المدبلج")


def main():
    audio_path = find_source_audio()
    if not audio_path:
        print("❌ ما فيه ملف صوت مصدري بمجلد work/ (لازم الـ workflow ينزّله من الـ Release أول)")
        sys.exit(1)

    print(f"🎧 ملف الصوت المصدري: {audio_path}")

    segments = transcribe(audio_path)
    if not segments:
        print("❌ ما قدر يستخرج أي كلام من الصوت (ممكن يكون صامت أو غير واضح)")
        sys.exit(1)

    translate_segments(segments)
    total_duration = get_duration_ms(audio_path)

    os.makedirs("output", exist_ok=True)
    dubbed_audio_path = os.path.join("output", "dubbed_audio.mp3")
    build_dubbed_audio(segments, total_duration, dubbed_audio_path)

    print(f"🎉 خلص! ملف الصوت المدبلج بمسار: {dubbed_audio_path}")


if __name__ == "__main__":
    main()
