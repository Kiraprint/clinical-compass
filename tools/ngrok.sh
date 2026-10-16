#!/usr/bin/env bash
# Стабильный публичный HTTPS-адрес демо через ngrok (free, статический dev-домен, авто-TLS).
# В отличие от cloudflared quick tunnel (URL меняется при каждом рестарте) адрес здесь статичен,
# но требует бесплатный аккаунт ngrok и ваш dev-домен. Исходящий трафик можно пустить через Xray.
#
# Подготовка (один раз):
#   ./tools/bin/ngrok config add-authtoken <TOKEN>   # token из https://dashboard.ngrok.com
#   export NGROK_URL=<ваш>.ngrok-free.app            # dev-домен из dashboard (статичный)
#   # опционально — если ISP режет даже HTTPS до ngrok, гнать исходящий через Xray:
#   export NGROK_PROXY=socks5://127.0.0.1:1080       # адрес локального SOCKS/HTTP прокси
#
# Использование:
#   ./tools/ngrok.sh                  — запустить и вывести стабильный URL
#   ./tools/ngrok.sh --stop           — остановить
#   ./tools/ngrok.sh status           — текущий URL
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/tools/bin/ngrok"
PIDFILE="$ROOT/tools/.ngrok.pid"
LOGFILE="$ROOT/tools/.ngrok.log"
URLFILE="$ROOT/tools/.ngrok.url"
UI_URL="http://127.0.0.1:8090"

require_ngrok_url() {
  if [[ -z "${NGROK_URL:-}" ]]; then
    echo "⚠️  Задай статический dev-домен: export NGROK_URL=<ваш>.ngrok-free.app" >&2
    echo "    Его показывает dashboard ngrok после логина." >&2
    exit 1
  fi
}

start() {
  require_ngrok_url
  NGROK_URL="${NGROK_URL#https://}"   # стерпим и полный URL, и голый домен
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Уже запущен: $(cat "$URLFILE" 2>/dev/null || echo '(URL ещё не готов)')"
    return 0
  fi

  if ! curl -s -o /dev/null --max-time 3 "$UI_URL"; then
    echo "⚠️  Стек не отвечает на $UI_URL. Сначала подними его:" >&2
    echo "    docker compose -f $ROOT/infra/docker-compose.yml up -d --build" >&2
    exit 1
  fi

  # Исходящий через Xray, если задан NGROK_PROXY (например, SOCKS5 локального xray-клиента).
  if [[ -n "${NGROK_PROXY:-}" ]]; then
    export http_proxy="$NGROK_PROXY" https_proxy="$NGROK_PROXY" all_proxy="$NGROK_PROXY"
  fi

  rm -f "$URLFILE" "$LOGFILE"
  "$BIN" http "$UI_URL" --url "https://${NGROK_URL}" --log=stdout >"$LOGFILE" 2>&1 &
  echo "$!" > "$PIDFILE"

  # Ждём появления публичного URL (обычно до 10с) в логе — печатаем его жирно.
  # Если процесс ngrok умер (нет authtoken / занят домен) — выходим сразу с понятной причиной.
  for _ in $(seq 1 60); do
    local url
    url="$(grep -oE 'https://[a-z0-9-]+\.ngrok(-free)?\.app' "$LOGFILE" 2>/dev/null | head -1 || true)"
    if [[ -n "$url" ]]; then
      echo "$url" > "$URLFILE"
      echo "✅ Публичный адрес демо (стабильный):"
      echo "   $url"
      return 0
    fi
    if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "⚠️  ngrok завершился с ошибкой. Причина:" >&2
      grep -E 'ERR_NGROK|ERROR' "$LOGFILE" 2>/dev/null | tail -n 3 >&2 || true
      echo "    Частые причины: не добавлен authtoken (ERR_NGROK_4018 → '$BIN config add-authtoken <TOKEN>')," >&2
      echo "    либо dev-домен занят другой сессией ngrok (ERR_NGROK_334)." >&2
      rm -f "$PIDFILE"
      exit 1
    fi
    sleep 0.5
  done
  # Если адрес не появился — подскажем причину: обычно неверный NGROK_URL или нет authtoken.
  echo "⚠️  URL не появился за 30с. Смотри лог: $LOGFILE" >&2
  tail -n 5 "$LOGFILE" >&2 || true
  exit 1
}

stop() {
  if [[ -f "$PIDFILE" ]]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE" "$URLFILE" "$LOGFILE"
    echo "ngrok остановлен."
  else
    echo "ngrok не запущен."
  fi
}

status() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Запущен: $(cat "$URLFILE" 2>/dev/null || echo '(URL ещё не готов)')"
  else
    echo "Не запущен."
  fi
}

case "${1:-start}" in
  --stop|stop) stop ;;
  status)      status ;;
  start|"")    start ;;
  *) echo "Использование: $0 [start|status|--stop]" >&2; exit 2 ;;
esac
