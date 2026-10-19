#!/bin/sh
# F7: восстановление БД из бэкапа.
# Usage: tools/restore.sh <file.sql.gz>
set -eu

COMPOSE="${COMPOSE:-infra/docker-compose.yml}"
FILE="${1:-}"

if [ -z "${FILE}" ] || [ ! -f "${FILE}" ]; then
  echo "Usage: $0 <file.sql.gz>"
  exit 1
fi

echo "[restore] gunzip ${FILE} → compass DB"
gunzip -c "${FILE}" | docker compose -f "${COMPOSE}" exec -T postgres \
  psql -U compass -d compass

echo "[restore] done — проверьте целостность:"
echo "  docker compose -f ${COMPOSE} run --rm seed --reset --days 14"
