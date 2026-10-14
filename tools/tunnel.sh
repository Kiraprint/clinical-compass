#!/usr/bin/env bash
# Демо-туннель Cloudflare (quick tunnel, 0 ₽, без аккаунта): даёт публичный HTTPS-URL на локальный стек.
# Использование:
#   ./tools/tunnel.sh            — запустить туннель и вывести публичный URL
#   ./tools/tunnel.sh --stop     — остановить туннель
#   ./tools/tunnel.sh status     — показать текущий URL (если туннель запущен)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/tools/bin/cloudflared"
PIDFILE="$ROOT/tools/.tunnel.pid"
LOGFILE="$ROOT/tools/.tunnel.log"
URLFILE="$ROOT/tools/.tunnel.url"
UI_URL="http://127.0.0.1:8090"

start() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Туннель уже запущен: $(cat "$URLFILE" 2>/dev/null || echo '(URL ещё не готов)')"
    return 0
  fi

  # Убедимся, что стек жив.
  if ! curl -s -o /dev/null --max-time 3 "$UI_URL"; then
    echo "⚠️  Стек не отвечает на $UI_URL. Сначала подними его:" >&2
    echo "   docker compose -f $ROOT/infra/docker-compose.yml up -d --build" >&2
    exit 1
  fi

  rm -f "$URLFILE" "$LOGFILE"
  nohup "$BIN" tunnel --url "$UI_URL" >"$LOGFILE" 2>&1 &
  echo "$!" > "$PIDFILE"

  # Ждём появления URL (обычно до 10с) из лога — печатаем его жирно.
  for _ in $(seq 1 40); do
    local url
    url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGFILE" 2>/dev/null | head -1 || true)"
    if [[ -n "$url" ]]; then
      echo "$url" > "$URLFILE"
      echo "✅ Публичный адрес демо:"
      echo "   $url"
      return 0
    fi
    sleep 0.5
  done
  echo "⚠️  URL не успел появиться за 20с. Смотри лог: $LOGFILE" >&2
  exit 1
}

stop() {
  if [[ -f "$PIDFILE" ]]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE" "$URLFILE" "$LOGFILE"
    echo "Туннель остановлен."
  else
    echo "Туннель не запущен."
  fi
}

status() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Запущен: $(cat "$URLFILE" 2>/dev/null || echo '(URL ещё не готов)')"
  else
    echo "Не запущен."
  fi
}

case "${1:-}" in
  --stop) stop ;;
  status) status ;;
  *) start ;;
esac
