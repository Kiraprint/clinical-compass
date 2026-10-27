#!/usr/bin/env bash
# Генерирует иконки и splash Android из mobile/assets/icon-mark.svg.
# Зависимости: rsvg-convert, ImageMagick 7 (magick).
set -euo pipefail
cd "$(dirname "$0")"
RES=../android/app/src/main/res
MARK=icon-mark.svg

render_mark() { # size out
  rsvg-convert -w "$1" -h "$1" "$MARK" -o "$2"
}

# Adaptive foreground: 108dp canvas, знак в безопасной зоне 66dp (61%).
for entry in mdpi:108 hdpi:162 xhdpi:216 xxhdpi:324 xxxhdpi:432; do
  d=${entry%%:*}; px=${entry##*:}; inner=$((px * 66 / 108))
  tmp=$(mktemp --suffix=.png)
  render_mark "$inner" "$tmp"
  magick -size "${px}x${px}" canvas:none "$tmp" -gravity center -composite -strip -define png:exclude-chunks=date,time \
    "$RES/mipmap-$d/ic_launcher_foreground.png"
  rm -f "$tmp"
done

# Legacy square и круглые иконки.
for entry in mdpi:48 hdpi:72 xhdpi:96 xxhdpi:144 xxxhdpi:192; do
  d=${entry%%:*}; px=${entry##*:}; inner=$((px * 72 / 100))
  tmp=$(mktemp --suffix=.png)
  render_mark "$inner" "$tmp"
  magick -size "${px}x${px}" xc:white "$tmp" -gravity center -composite -strip -define png:exclude-chunks=date,time \
    "$RES/mipmap-$d/ic_launcher.png"
  magick -size "${px}x${px}" xc:none -fill white -draw "circle $((px/2)),$((px/2)) $((px/2)),1" \
    "$tmp" -gravity center -composite -strip -define png:exclude-chunks=date,time "$RES/mipmap-$d/ic_launcher_round.png"
  rm -f "$tmp"
done

# Splash: белый фон, знак 22% меньшей стороны. Размеры повторяют Capacitor-шаблон.
for entry in drawable:480x320 drawable-land-mdpi:480x320 drawable-land-hdpi:800x480 \
  drawable-land-xhdpi:1280x720 drawable-land-xxhdpi:1600x960 drawable-land-xxxhdpi:1920x1280 \
  drawable-port-mdpi:320x480 drawable-port-hdpi:480x800 drawable-port-xhdpi:720x1280 \
  drawable-port-xxhdpi:960x1600 drawable-port-xxxhdpi:1280x1920; do
  dir=${entry%%:*}; size=${entry##*:}; w=${size%x*}; h=${size#*x}
  inner=$(( (w < h ? w : h) * 22 / 100 ))
  tmp=$(mktemp --suffix=.png)
  render_mark "$inner" "$tmp"
  magick -size "${w}x${h}" xc:white "$tmp" -gravity center -composite -strip -define png:exclude-chunks=date,time "$RES/$dir/splash.png"
  rm -f "$tmp"
done
