#!/data/data/com.termux/files/usr/bin/bash
# يحمّل فيديو من يوتيوب محليًا، يفصل الصوت، يرفع الصوت بس (خفيف) كإصدار
# مؤقت على GitHub، يشغّل عملية الدبلجة، يرجّع الصوت المدبلج، ويدمجه محليًا
# مع الفيديو الأصلي بدون ما يحتاج يرفع أو ينزّل الفيديو الكامل أبدًا.
set -e

YOUTUBE_URL="$1"
WHISPER_MODEL="${2:-small}"

if [ -z "$YOUTUBE_URL" ]; then
  echo "الاستخدام: bash dub_from_phone.sh <رابط_يوتيوب> [tiny|base|small|medium]"
  exit 1
fi

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
TAG="audio-$(date +%s)"
WORK="$HOME/dub_local/$TAG"
mkdir -p "$WORK"

VIDEO_FILE="$WORK/source_video.mp4"
AUDIO_FILE="$WORK/source_audio.mp3"
DUBBED_AUDIO_FILE="$WORK/dubbed_audio.mp3"
FINAL_FILE="$HOME/storage/downloads/dubbed_$(date +%Y%m%d_%H%M%S).mp4"

echo "📦 المستودع: $REPO"

echo "⬇️  (1/6) جاري تحميل الفيديو محليًا على الجهاز..."
yt-dlp -f "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best" \
  --merge-output-format mp4 -o "$VIDEO_FILE" "$YOUTUBE_URL"

if [ ! -f "$VIDEO_FILE" ]; then
  echo "❌ فشل تحميل الفيديو"
  exit 1
fi
echo "   حجم الفيديو: $(du -h "$VIDEO_FILE" | cut -f1)"

echo "🎧 (2/6) جاري فصل الصوت من الفيديو..."
ffmpeg -y -i "$VIDEO_FILE" -vn -ar 16000 -ac 1 -b:a 64k "$AUDIO_FILE" \
  -loglevel error
if [ ! -f "$AUDIO_FILE" ]; then
  echo "❌ فشل فصل الصوت"
  exit 1
fi
echo "   حجم الصوت: $(du -h "$AUDIO_FILE" | cut -f1) (هذا اللي بيترفع، مو الفيديو)"

echo "📤 (3/6) جاري رفع الصوت فقط كإصدار مؤقت على GitHub..."
gh release create "$TAG" "$AUDIO_FILE" \
  --repo "$REPO" \
  --title "Temp audio $TAG" \
  --notes "رفع مؤقت لمعالجة الدبلجة، يُحذف تلقائيًا بعد الانتهاء"

echo "🚀 (4/6) جاري تشغيل عملية الدبلجة على GitHub Actions..."
gh workflow run dub.yml --repo "$REPO" \
  -f audio_release_tag="$TAG" \
  -f whisper_model="$WHISPER_MODEL"

sleep 8
echo "👀 جاري متابعة التشغيل..."
gh run watch --repo "$REPO"

echo "⬇️  (5/6) جاري تنزيل الصوت المدبلج (ملف صغير)..."
gh run download --repo "$REPO" -n dubbed-audio -D "$WORK/"
mv "$WORK"/dubbed_audio.mp3 "$DUBBED_AUDIO_FILE" 2>/dev/null || true

if [ ! -f "$DUBBED_AUDIO_FILE" ]; then
  echo "❌ ما لقيت الصوت المدبلج، تأكد إن التشغيلة نجحت"
  exit 1
fi

echo "🎬 (6/6) جاري دمج الصوت المدبلج مع الفيديو الأصلي محليًا..."
ffmpeg -y -i "$VIDEO_FILE" -i "$DUBBED_AUDIO_FILE" \
  -c:v copy -map 0:v:0 -map 1:a:0 -shortest "$FINAL_FILE" \
  -loglevel error

echo "🧹 جاري تنظيف الملفات المؤقتة..."
rm -rf "$WORK"

echo ""
echo "🎉 خلص! الفيديو المدبلج موجود هنا:"
echo "   $FINAL_FILE"
