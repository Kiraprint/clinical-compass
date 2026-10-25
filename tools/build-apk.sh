#!/usr/bin/env bash
# Локальная сборка release-APK через CLI в Docker-образе compass-android-builder.
# На машине нет Java/Android SDK/gradle — всё внутри образа (JDK 21 + SDK 36).
#
# Использование:
#   tools/build-apk.sh                 # версия из mobile/android/app/build.gradle (1.0)
#   CC_VERSION_NAME=1.1.0 tools/build-apk.sh      # versionName 1.1.0
#   tools/build-apk.sh 1.1.0           # то же самое, versionCode считается 10100
#   CC_VERSION_CODE=10100 …            # явный versionCode
#   CC_API_BASE=http://127.0.0.1:8090  # другой API-хост для SPA
#
# Подпись: demo-keystore генерируется один раз (keytool) и переиспользуется,
# чтобы обновления APK ставились поверх прежнего. Пароли — demo, не для_store.
# Схема версий (как в release-android.yml): vX.Y.Z → X.Y.Z / X*10000+Y*100+Z.
set -euo pipefail

cd "$(dirname "$0")/.."
IMG=compass-android-builder
ANDROID_DIR=mobile/android
KEYSTORE="$ANDROID_DIR/app/release.keystore"
PROPS="$ANDROID_DIR/keystore.properties"
STOREPASS=compass-release
ALIAS=compass
KEYPASS=compass-release
API_BASE="${CC_API_BASE:-https://compass.fxtun.ru}"

VERSION_NAME="${1:-${CC_VERSION_NAME:-}}"
VERSION_CODE="${CC_VERSION_CODE:-}"
if [ -n "$VERSION_NAME" ]; then
  if [ -z "$VERSION_CODE" ]; then
    if [[ "$VERSION_NAME" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
      VERSION_CODE=$(( BASH_REMATCH[1] * 10000 + BASH_REMATCH[2] * 100 + BASH_REMATCH[3] ))
    else
      echo "[apk] версия '$VERSION_NAME' не в формате X.Y.Z — задайте CC_VERSION_CODE" >&2
      exit 1
    fi
  fi
fi
LABEL="${VERSION_NAME:-release}"

if ! docker image inspect "$IMG" >/dev/null 2>&1; then
  echo "[apk] образ $IMG не найден — собери: docker build -t $IMG tools/android-builder" >&2
  exit 1
fi

# --- 1. SPA: собрать с нужным API-хостом и отдать Capacitor ------------------
echo "[apk] VITE_API_BASE=$API_BASE → ui/dist"
if [ ! -d ui/node_modules ]; then
  echo "[apk] ui/node_modules отсутствует — npm ci" >&2
  (cd ui && npm ci)
fi
(cd ui && VITE_API_BASE="$API_BASE" npm run build)

if [ ! -d mobile/node_modules ]; then
  echo "[apk] mobile/node_modules отсутствует — npm ci" >&2
  (cd mobile && npm ci)
fi
(cd mobile && npx cap sync android)

# --- 2. Keystore -------------------------------------------------------------
# Генерируем внутри контейнера (keytool из JDK), файл персистентен на хосте.
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

# --- 3. Gradle ---------------------------------------------------------------
GRADLE_ARGS=(assembleRelease --no-daemon)
if [ -n "$VERSION_NAME" ]; then
  GRADLE_ARGS+=("-PccVersionName=$VERSION_NAME" "-PccVersionCode=$VERSION_CODE")
  echo "[apk] versionName=$VERSION_NAME versionCode=$VERSION_CODE"
fi
echo "[apk] gradlew ${GRADLE_ARGS[*]}"
docker run --rm -v "$PWD:/work" -w /work/mobile/android "$IMG" ./gradlew "${GRADLE_ARGS[@]}"

APK="$ANDROID_DIR/app/build/outputs/apk/release/app-release.apk"
if [ ! -f "$APK" ]; then
  echo "[apk] APK не найден по пути $APK" >&2
  exit 1
fi
OUT="dist/compass-${LABEL}-release.apk"
mkdir -p dist
cp "$APK" "$OUT"
echo "[apk] готов: $OUT  ($(du -h "$OUT" | cut -f1))"

# --- 4. Проверка (подпись + версия) -----------------------------------------
docker run --rm -v "$PWD/dist:/apk:ro" "$IMG" bash -c '
  BT=$(ls -d /opt/android-sdk/build-tools/* | sort -V | tail -1)
  "$BT/aapt2" dump badging "/apk/'"$(basename "$OUT")"'" 2>/dev/null | grep -E "^package:" || true
'
