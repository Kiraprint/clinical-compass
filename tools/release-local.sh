#!/usr/bin/env bash
# Локальный релиз clinical-compass (GH Actions не используем: лимиты).
#   тесты → docker-образы api/ui → release-APK → git-тег → GitHub Release (APK).
#
# Использование:
#   tools/release-local.sh 1.1.0                    # всё по порядку
#   tools/release-local.sh 1.1.0 --skip-ci          # тесты уже гоняли
#   tools/release-local.sh 1.1.0 --no-images        # только APK+Release
#   tools/release-local.sh 1.1.0 --no-tag           # без git-тега
#   tools/release-local.sh 1.1.0 --push-images      # + docker push в GHCR
#   tools/release-local.sh 1.1.0 --notes-file f.md  # свой текст релиза
#
# GH нужен только как хранилище релиза: APK заливается через gh release.
# Образы в GHCR — опция: нужен токен со scope write:packages
#   gh auth refresh -h github.com -s write:packages      (интерактивно)
#   либо PAT с write:packages:  echo $PAT | docker login ghcr.io -u <user> --password-stdin
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-}"
[ -n "$VERSION" ] || { sed -n '2,14p' "$0"; exit 2; }
shift || true
[[ "$VERSION" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || { echo "версия должна быть X.Y.Z" >&2; exit 2; }
VERSION_CODE=$(( BASH_REMATCH[1] * 10000 + BASH_REMATCH[2] * 100 + BASH_REMATCH[3] ))
TAG="v$VERSION"

SKIP_CI=0; DO_IMAGES=1; DO_TAG=1; PUSH_IMAGES=0; NOTES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-ci) SKIP_CI=1 ;;
    --no-images) DO_IMAGES=0 ;;
    --no-tag) DO_TAG=0 ;;
    --push-images) PUSH_IMAGES=1 ;;
    --notes-file) shift; NOTES="${1:-}" ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
  shift
done

REGISTRY="${CC_REGISTRY:-ghcr.io/kiraprint/clinical-compass}"
API_IMAGE="$REGISTRY/compass-api"
UI_IMAGE="$REGISTRY/compass-ui"
APK="dist/compass-${VERSION}-release.apk"
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mОТКАЗ:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 0. Дерево ---------------------------------------------------------------
gh auth status >/dev/null 2>&1 || fail "gh не авторизован — залить ассет релиза некуда"
git fetch --quiet origin main || fail "не удалось обновить origin/main"
[ -z "$(git status --porcelain)" ] || fail "рабочее дерево грязное — релиз собирается только из коммита"
log "релиз $TAG (versionCode $VERSION_CODE) из $(git rev-parse --short HEAD)"

# --- 1. Тесты ----------------------------------------------------------------
if [ "$SKIP_CI" = 0 ]; then
  log "локальный CI"
  tools/ci.sh
else
  log "CI пропущен (--skip-ci)"
fi

# --- 2. Образы ---------------------------------------------------------------
if [ "$DO_IMAGES" = 1 ]; then
  log "docker build api"
  docker build -f api/Dockerfile -t "$API_IMAGE:$TAG" -t "$API_IMAGE:latest" .
  log "docker build ui"
  docker build -f ui/Dockerfile -t "$UI_IMAGE:$TAG" -t "$UI_IMAGE:latest" .
  if [ "$PUSH_IMAGES" = 1 ]; then
    for img in "$API_IMAGE:$TAG" "$API_IMAGE:latest" "$UI_IMAGE:$TAG" "$UI_IMAGE:latest"; do
      log "push $img"
      docker push "$img" || fail "push отклонён. Нужен токен со scope write:packages:
  gh auth refresh -h github.com -s write:packages   (интерактивно)
  или PAT: echo \$PAT | docker login ghcr.io -u <user> --password-stdin"
    done
  else
    log "образы собраны локально; в GHCR не льём (--push-images чтобы включить)"
  fi
fi

# --- 3. APK ------------------------------------------------------------------
log "сборка APK (VITE_API_BASE=${CC_API_BASE:-https://compass.fxtun.ru})"
CC_API_BASE="${CC_API_BASE:-https://compass.fxtun.ru}" tools/build-apk.sh "$VERSION"
[ -f "$APK" ] || fail "APK не появился: $APK"

# --- 4. Тег ------------------------------------------------------------------
if [ "$DO_TAG" = 1 ]; then
  HEAD_SHA="$(git rev-parse HEAD)"
  if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    # Повторный прогон после сбоя на шаге Release: тег обязан указывать на HEAD,
    # иначе артефакт и тег разъедутся молча.
    [ "$(git rev-parse "$TAG^{commit}")" = "$HEAD_SHA" ] \
      || fail "тег $TAG указывает на другой коммит — возьмите следующую версию"
    log "тег $TAG уже есть и указывает на HEAD — продолжаю"
  else
    if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
      fail "тег $TAG уже есть на origin — возьмите следующую версию"
    fi
    [ "$HEAD_SHA" = "$(git rev-parse origin/main)" ] \
      || fail "HEAD не совпадает с origin/main — тег ставится только на main после мержа"
    git tag -a "$TAG" -m "Компас $VERSION"
    git push origin "$TAG"
    log "тег $TAG запушен"
  fi
fi

# --- 5. GitHub Release (только APK) -----------------------------------------
if gh release view "$TAG" >/dev/null 2>&1; then
  log "обновляю ассет в существующем релизе $TAG"
  gh release upload "$TAG" "$APK" --clobber
else
  log "создаю релиз $TAG"
  if [ -n "$NOTES" ]; then
    gh release create "$TAG" --title "Компас $VERSION" --notes-file "$NOTES" "$APK"
  else
    gh release create "$TAG" --title "Компас $VERSION" --generate-notes "$APK"
  fi
fi
gh release view "$TAG" --json assets -q '"ассеты: " + ([.assets[].name] | join(", "))'
log "готово: $(gh release view "$TAG" --json url -q .url)"
