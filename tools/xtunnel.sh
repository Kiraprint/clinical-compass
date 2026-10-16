#!/usr/bin/env bash
# Российский аналог ngrok для демо (RF-resident endpoint, свой TLS, домен *.tunnel4.com).
# ВАЖНО по тарифам xtunnel: БЕСПЛАТНЫЙ тариф даёт ВРЕМЕННЫЙ публичный адрес (адрес может меняться);
# ПОСТОЯННЫЙ адрес и собственный домен — на платных тарифах (Личный 99₽/мес, Профессиональный 299₽/мес).
# Параметр --name: рестарты возвращаются к тому же туннелю и URL (проверить на своём тарифе).
#
# vs ngrok.sh: ngrok free даёт СТАТИЧЕСКИЙ dev-домен бесплатно, но это зарубежный сервис (US);
# xtunnel — российский сервис (удобнее для РФ-жюри/резидентности), но бесплатный адрес — временный.
# Держим оба: выбери тот, что реально держит адрес стабильным на демо.
#
# Подготовка (один раз):
#   # бинарник + сертификат кладутся в tools/bin/ (gitignored), либо скачайте вручную:
#   #   curl -L -o tools/bin/xtunnel.tar.gz https://dl.xtunnel.ru/v4.3.0/certfix1/xtunnel-v4.3.0-linux-x64.tar.gz
#   #   tar xzf tools/bin/xtunnel.tar.gz -C tools/bin
#   export XTUNNEL_KEY=<ключ>            # из https://cabinet.xtunnel.ru (FREE_KEY / PERSONAL_KEY / ...)
#   # опционально:
#   export XTUNNEL_NAME=compass          # имя туннеля: рестарты -> тот же URL (см. тариф)
#   export XTUNNEL_PORT=8090             # локальный порт демо
#
# Использование:
#   ./tools/xtunnel.sh          — запустить и вывести публичный URL
#   ./tools/xtunnel.sh --stop   — остановить
#   ./tools/xtunnel.sh status   — текущий URL
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/tools/bin/xtunnel"
PIDFILE="$ROOT/tools/.xtunnel.pid"
LOGFILE="$ROOT/tools/.xtunnel.log"
URLFILE="$ROOT/tools/.xtunnel.url"
UI_URL="http://127.0.0.1:${XTUNNEL_PORT:-8090}"

require_key() {
  if [[ -z "${XTUNNEL_KEY:-}" ]]; then
    echo "⚠️  Не задан XTUNNEL_KEY (ключ лицензии из https://cabinet.xtunnel.ru)." >&2
    echo "    export XTUNNEL_KEY=<ключ>" >&2
    exit 1
  fi
}

start() {
  require_key
  if [[ ! -x "$BIN" ]]; then
    echo "⚠️  Бинарник xtunnel не найден: $BIN" >&2
    echo "    Скачай и распакуй: curl -L -o $ROOT/tools/bin/xtunnel.tar.gz \\" >&2
    echo "      https://dl.xtunnel.ru/v4.3.0/certfix1/xtunnel-v4.3.0-linux-x64.tar.gz" >&2
    echo "    и tar xzf $ROOT/tools/bin/xtunnel.tar.gz -C $ROOT/tools/bin" >&2
    exit 1
  fi
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "xtunnel уже запущен: $(cat "$URLFILE" 2>/dev/null || echo '(URL ещё не готов)')"
    return 0
  fi
  if ! curl -s -o /dev/null --max-time 3 "$UI_URL"; then
    echo "⚠️  Демо не поднято на $UI_URL. Запусти docker compose up -d сначала." >&2
    exit 1
  fi

  rm -f "$URLFILE" "$LOGFILE"
  local args=(http "${XTUNNEL_PORT:-8090}" --license "$XTUNNEL_KEY") 
  if [[ -n "${XTUNNEL_NAME:-}" ]]; then
    args+=(--name "$XTUNNEL_NAME")
  fi
  # --monitor=false: без интерактивного TUI; --output-mode plain: URL в stdout.
  "$BIN" "${args[@]}" --monitor=false --output-mode plain >"$LOGFILE" 2>&1 &
  echo "$!" > "$PIDFILE"

  # Ждём появления публичного URL в логе. Если процесс умер — выходим сразу с причиной.
  for _ in $(seq 1 60); do
    local url
    url="$(grep -oE 'https://[a-z0-9.-]+\.tunnel4\.com' "$LOGFILE" 2>/dev/null | head -1 || true)"
    if [[ -n "$url" ]]; then
      echo "$url" > "$URLFILE"
      echo "✅ Публичный адрес демо (xtunnel):"
      echo "   $url"
      return 0
    fi
    if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "⚠️  xtunnel завершился с ошибкой. Причина:" >&2
      tail -n 8 "$LOGFILE" 2>/dev/null >&2 || true
      echo "    Частые причины: неверный/неактивный XTUNNEL_KEY (безопасный ключ нужен из кабинета)," >&2
      echo "    либо ключ занят другой сессией (тогда укажи --name или освободи туннель в кабинете)." >&2
      rm -f "$PIDFILE"
      exit 1
    fi
    sleep 0.5
  done
  echo "⚠️  URL не появился за 30с. Смотри лог: $LOGFILE" >&2
  tail -n 8 "$LOGFILE" >&2 || true
  exit 1
}

stop() {
  if [[ -f "$PIDFILE" ]]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE" "$URLFILE" "$LOGFILE"
    echo "xtunnel остановлен."
  else
    echo "xtunnel не запущен."
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
