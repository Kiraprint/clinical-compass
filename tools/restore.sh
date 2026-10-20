#!/bin/sh
# F7: восстановление БД из зашифрованного бэкапа.
# Usage: CC_BACKUP_KEY=... tools/restore.sh <file.sql.gz.enc>
set -eu

COMPOSE="${COMPOSE:-infra/docker-compose.yml}"
FILE="${1:-}"

if [ -z "${FILE}" ] || [ ! -f "${FILE}" ]; then
  echo "Usage: CC_BACKUP_KEY=... $0 <file.sql.gz.enc>"
  exit 1
fi
if [ -z "${CC_BACKUP_KEY:-}" ]; then
  echo "[restore] ОШИБКА: CC_BACKUP_KEY не задан (бэкап зашифрован AES-256)." >&2
  exit 1
fi

echo "[restore] decrypt AES-256 → gunzip → compass DB"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
    -pass env:CC_BACKUP_KEY -in "${FILE}" \
  | gunzip -c \
  | docker compose -f "${COMPOSE}" exec -T postgres psql -U compass -d compass

echo "[restore] done — проверьте целостность:"
echo "  docker compose -f ${COMPOSE} run --rm seed --reset --days 14"
