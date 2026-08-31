#!/data/data/com.termux/files/usr/bin/bash
# يشغّل واجهة الويب المحلية للدبلجة، مع قفل استيقاظ (wake-lock) عشان
# أندرويد ما يقتل العملية إذا سكّرت الشاشة أو سيّبت الجهاز.
set -e

cd "$(dirname "$0")"

echo "🔒 منع الجهاز من النوم أثناء المعالجة..."
termux-wake-lock

echo "🌐 تشغيل السيرفر المحلي..."
echo "   افتح المتصفح على: http://127.0.0.1:8890"
echo "   (اضغط Ctrl+C لإيقاف السيرفر وتحرير الجهاز من قفل الاستيقاظ)"
echo ""

trap 'echo ""; echo "🔓 تحرير قفل الاستيقاظ..."; termux-wake-unlock' EXIT

python app.py
