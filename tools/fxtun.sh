#!/usr/bin/env bash
# fxTunnel (fxtun.ru) — публичный туннель демо в Docker.
# Агент в Docker обязателен: на этой машине transparent-proxy xray (fake-ip + TUN)
# ломает хостовые туннели (mux/QUIC), а контейнер выходит напрямую через FORWARD.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$ROOT/tools/.fxtun.env"
IMAGE="compass-fxtun-client"
NAME="fxtun-demo"
PORT="${FXTUNNEL_PORT:-8090}"
DOMAIN="${FXTUNNEL_DOMAIN:-compass}"

URL_FILE="$ROOT/tools/.fxtun.url"

log()  { printf '%s\n' "$*"; }

# Tокен/домен из окружения или tools/.fxtun.env (gitignored).
load_conf() {
    [ -f "$CONF" ] && . "$CONF"
    if [ -z "${FXTUNNEL_TOKEN:-}" ]; then
        log "⚠️  Не задан FXTUNNEL_TOKEN (ключ из https://fxtun.ru)."
        log "    export FXTUNNEL_TOKEN=<ключ>   или создай tools/.fxtun.env:"
        log "    FXTUNNEL_TOKEN=sk_..."
        exit 1
    fi
}

build_image() {
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        log "Собираю образ $IMAGE..."
        docker build -q -t "$IMAGE" "$ROOT/tools/fxtun-docker" >/dev/null
    fi
}

public_url() {
    local u
    u=$(docker logs "$NAME" 2>&1 | grep -oE 'https://[a-z0-9.-]+' | tail -1 || true)
    printf '%s' "$u"
}

start() {
    load_conf; build_image
    if docker inspect "$NAME" >/dev/null 2>&1 && docker ps --filter "name=^/${NAME}$" --format '{{.Names}}' | grep -q . ; then
        log "Уже запущен: $(public_url)"
        return 0
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    # Проверяем, что локальный демо-стек жив (сам туннель проксирует на него).
    if ! curl -s -o /dev/null --max-time 3 "http://127.0.0.1:${PORT}/" ; then
        log "⚠️  Локальный сервис не отвечает на :${PORT} (запусти docker compose up -d)."
        exit 1
    fi
    log "Старт туннеля (Docker-агент, egress напрямую мимо xray)..."
    docker run -d --name "$NAME" --restart=unless-stopped \
        --add-host host.docker.internal:host-gateway \
        -e FXTUNNEL_TOKEN="$FXTUNNEL_TOKEN" \
        -e FXTUNNEL_LOCAL_PORT="$PORT" \
        "$IMAGE" -d "$DOMAIN" --no-inspect --log-format json --log-level info >/dev/null

    # Ждём URL (агент печатает 'HTTP tunnel created' за ~1-3с).
    local url=""
    for _ in $(seq 1 20); do
        url=$(public_url)
        [ -n "$url" ] && break
        sleep 1
    done
    if [ -z "$url" ]; then
        log "❌ Туннель не поднялся. Логи:"
        docker logs --tail 20 "$NAME" 2>&1 | sed 's/^/    /'
        exit 1
    fi
    printf '%s\n' "$url" > "$URL_FILE"
    log "✅ Публичный адрес демо (fxTunnel, Docker):"
    log "   $url"
}

status() {
    if docker inspect "$NAME" >/dev/null 2>&1; then
        log "Запущен: $(public_url)"
    else
        log "Не запущен."
    fi
}

stop() {
    docker rm -f "$NAME" >/dev/null 2>&1 && log "Остановлен: $NAME" || log "Не запущен."
    rm -f "$URL_FILE"
}

case "${1:-start}" in
    start)   start ;;
    status)  status ;;
    --stop|stop) stop ;;
    *) log "Использование: $0 {start|status|--stop}" ;;
esac
