#!/usr/bin/env bash
# Локальная сборка release-APK (item 4) через CLI в Docker-образе compass-android-builder.
# На машине нет Java/Android SDK/gradle — всё внутри образа (JDK 21 + SDK 36).
#
# Подпись: demo-keystore генерируется один раз (keytool) и переиспользуется,
# чтобы обновления APK ставились поверх прежнего. Пароли — demo, не для_store.
# API-хост: bake в ui/dist через VITE_API_BASE перед `cap sync` (см. README).
set -euo pipefail

cd "$(dirname "$0")/.."
IMG=compass-android-builder
ANDROID_DIR=mobile/android
KEYSTORE="$ANDROID_DIR/app/release.keystore"
PROPS="$ANDROID_DIR/keystore.properties"
STOREPASS=compass-release
ALIAS=compass
KEYPASS=compass-release

if ! docker image inspect "$IMG" >/dev/null 2>&1; then
  echo "[apk] образ $IMG не найден — собери: docker build -t $IMG tools/android-builder" >&2
  exit 1
fi

# Keystore генерируем внутри контейнера (keytool из JDK), файл персистентен на хосте.
if [ ! -f "$KEYSTORE" ]; then
  echo "[apk] генерирую release-keystore: $KEYSTORE"
  docker run --rm -v "$PWD:/work" -w /work "$IMG" \
    keytool -genkeypair -v -keystore "/work/$KEYSTORE" -alias "$ALIAS" \
      -storepass "$STOREPASS" -keypass "$KEYPASS" -keyalg RSA -keysize 2048 -validity 10000 \
      -dname "CN=Compass Demo, O=compass, C=RU"
fi

cat > "$PROPS" <<EOF
storeFile=app/release.keystore
storePassword=$STOREPASS
keyAlias=$ALIAS
keyPassword=$KEYPASS
EOF

echo "[apk] gradlew assembleRelease"
docker run --rm -v "$PWD:/work" -w /work/mobile/android "$IMG" \
  ./gradlew assembleRelease --no-daemon

APK="$ANDROID_DIR/app/build/outputs/apk/release/app-release.apk"
if [ ! -f "$APK" ]; then
  echo "[apk] APK не найден по пути $APK" >&2
  exit 1
fi
OUT="dist/compass-release.apk"
mkdir -p dist
cp "$APK" "$OUT"
echo "[apk] готов: $OUT  ($(du -h "$OUT" | cut -f1))"
