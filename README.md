# أداة دبلجة فيديوهات يوتيوب عبر GitHub Actions

تحمّل فيديو من يوتيوب بأي لغة، تفرّغ الكلام لنص، تترجمه للعربية، تولّد صوت
عربي مطابق للتوقيت، وتدمجه مع الفيديو — كل هذا يشتغل على سيرفرات GitHub
Actions مجانًا، وأنت بس ترسل الأمر من جوالك.

## الإعداد (مرة وحدة بس)

راجع ملف [SETUP.md](./SETUP.md) للتفاصيل الكاملة خطوة بخطوة.

## الاستخدام اليومي (من ترمكس)

```bash
gh workflow run dub.yml \
  -f youtube_url="https://youtu.be/XXXXXXX" \
  -f whisper_model="small"

gh run watch

gh run download -n dubbed-video -D ~/storage/downloads/
```

## خط الأنابيب (Pipeline)

1. `yt-dlp` يحمّل الفيديو بأفضل جودة (حد أقصى 1080p)
2. `ffmpeg` يفصل الصوت
3. `faster-whisper` يفرّغ الكلام لنص مع كشف اللغة الأصلية تلقائيًا
4. `deep-translator` (Google Translate) يترجم كل جملة للعربية
5. `gTTS` يولّد صوت عربي لكل جملة، ويتم ضبط سرعته ليقارب مدة الجملة الأصلية
6. `ffmpeg` يدمج الصوت الجديد مع الفيديو الأصلي

## الملفات

- `.github/workflows/dub.yml` — تعريف الـ workflow
- `scripts/dub.py` — منطق الدبلجة الكامل
- `requirements.txt` — مكتبات بايثون المطلوبة
- `SETUP.md` — دليل الإعداد الكامل
