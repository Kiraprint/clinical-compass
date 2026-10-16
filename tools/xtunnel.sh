#!/usr/bin/env bash
# Россияский аналог ngrok для демо (RF-resident endpoint, свой TLS, домен *.tunnel4.com).
#
# ВАЖНО (проверено живьём 16.10): агент xtunnel на ЭТОЙ машине нельзя запускать как голый
# бинарник — машина стоит под transparent-proxy xray (fake-ip + TUN throne-tun), и соединения
# хостовых процессов уходят через зарубежный выход: каждый проброшенный запрос ~135с.
# Тот же агент в Docker-контейнере (egress через FORWARD, минуя host-TUN) отвечает за ~1с.
# Поэтому скрипт по умолчанию запускает агент В Docker (образ ghcr.io/xtnnl/xtunnel-agent).
#
# Альтернатива без Docker: добавить в конфиг xray прямое правило для tunnel4.com
# (domain -> direct, DNS без fake-ip) — тогда заработает и хостовый бинарник.
#
# Тарифы xtunnel: бесплатный даёт UUID-адрес https://<uuid>.tunnel4.com — по факту стабилен
# между рестартами (проверено); постоянный именной адрес/свой домен — платные тарифы.
#
# Подготовка (один раз):
#   export XTUNNEL_KEY=<ключ>      # из https://cabinet.xtunnel.ru (FREE_KEY / PERSONAL_KEY / ...)
#   # опционально:
#   export XTUNNEL_PORT=8090       # локальный порт демо (по умолчанию 8090)
#
# Использование:
#   ./tools/xtunnel.sh          — запустить (Docker) и вывести публичный URL
#   ./tools/xtunnel.sh --stop   — остановить контейнер
#   ./tools/xtunnel.sh status   — текущий URL
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CNAME="xtunnel-demo"
IMAGE="ghcr.io/xtnnl/xtunnel-agent:v4.3.0"
URLFILE="$ROOT/tools/.xtunnel.url"
PORT="${XTUNNEL_PORT:-8090}"

require_key() {
  if [[ -z "${XTUNNEL_KEY:-}" ]]; then
    echo "⚠️  Не задан XTUNNEL_KEY (ключ лицензии из https://cabinet.xtunnel.ru)." >&2
    echo "    export XTUNNEL_KEY=<ключ>" >&2
    exit 1
  fi
}

url_from_logs() {
  docker logs "$CNAME" 2>&1 | grep -oE 'https://[a-z0-9-]+\.tunnel4\.com' | head -1 || true
}

start() {
  require_key
  if [[ "$(docker ps -q -f name="^${CNAME}$" 2>/dev/null)" != "" ]]; then
    echo "xtunnel уже запущен: $(cat "$URLFILE" 2>/dev/null || url_from_logs)"
    return 0
  fi
  if ! curl -s -o /dev/null --max-time 3 "http://127.0.0.1:${PORT}"; then
    echo "⚠️  Демо не поднято на http://127.0.0.1:${PORT}. Запусти docker compose up -d сначала." >&2
    exit 1
  fi
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Скачиваю образ агента ($IMAGE)..."
    docker pull "$IMAGE"
  fi

  rm -f "$URLFILE"
  # —add-host: host.docker.internal указывает на хост (Linux).
  # -v xtunnel-identity: сохранить NodeId агента между пересозданиями контейнера.
  docker rm -f "$CNAME" >/dev/null 2>&1 || true
  docker run -d --name "$CNAME" \
    --restart=unless-stopped \
    --add-host=host.docker.internal:host-gateway \
    -v xtunnel-identity:/var/lib/xtunnel \
    -e XTUNNEL_LICENSE="$XTUNNEL_KEY" \
    -e XTUNNEL_TARGET="http://host.docker.internal:${PORT}" \
    "$IMAGE" >/dev/null

  for _ in $(seq 1 60); do
    local url
    url="$(url_from_logs)"
    if [[ -n "$url" ]]; then
      echo "$url" > "$URLFILE"
      echo "✅ Публичный адрес демо (xtunnel, Docker):"
      echo "   $url"
      return 0
    fi
    if ! docker ps -q -f name="^${CNAME}$" | grep -q .; then
      echo "⚠️  Контейнер xtunnel завершился с ошибкой. Лог:" >&2
      docker logs "$CNAME" 2>&1 | tail -n 10 >&2 || true
      echo "    Частые причины: неверный/неактивный XTUNNEL_KEY, либо ключ занят другой сессией." >&2
      exit 1
    fi
    sleep 0.5
  done
  echo "⚠️  URL не появился за 30с. Лог контейнера:" >&2
  docker logs "$CNAME" 2>&1 | tail -n 10 >&2 || true
  exit 1
}

stop() {
  if docker ps -a -q -f name="^${CNAME}$" | grep -q .; then
    docker rm -f "$CNAME" >/dev/null
    rm -f "$URLFILE"
    echo "xtunnel (Docker) остановлен."
  else
    echo "xtunnel не запущен."
  fi
}

status() {
  if docker ps -q -f name="^${CNAME}$" | grep -q .; then
    echo "Запущен: $(cat "$URLFILE" 2>/dev/null || url_from_logs)"
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