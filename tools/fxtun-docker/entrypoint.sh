#!/bin/sh
set -e

# fxtunnel проксирует публичный URL на 127.0.0.1:$LOCAL_PORT внутри контейнера,
# поэтому поднимаем socat-релей до нашего приложения на хосте (host.docker.internal).
LOCAL_PORT="${FXTUNNEL_LOCAL_PORT:-8090}"
TARGET="${FXTUNNEL_TARGET:-host.docker.internal:8090}"

socat TCP-LISTEN:${LOCAL_PORT},fork,reuseaddr TCP:${TARGET} &
SOCAT_PID=$!
trap 'kill $SOCAT_PID 2>/dev/null || true' EXIT

exec /usr/local/bin/fxtunnel http "${LOCAL_PORT}" "$@"
