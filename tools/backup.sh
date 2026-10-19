#!/bin/sh
# F7 (ФСТЭК №21; ПП РФ №1119; 152-ФЗ ст.10): резервное копирование БД clinical-compass.
# pg_dump → gzip → AES-256 (openssl enc) → tools/backups/compass-YYYYMMDD-HHMMSS.sql.gz.enc
# Ключ: CC_BACKUP_KEY (env, >=32 байт) — вне git; без ключа скрипт отказывается работать.
# Cron: 0 3 * * * CC_BACKUP_KEY=... /home/kir/Code/clinical-compass/tools/backup.sh
# Восстановление: tools/restore.sh <file.sql.gz.enc>
set -eu

COMPOSE="${COMPOSE:-infra/docker-compose.yml}"
BACKUP_DIR="${BACKUP_DIR:-tools/backups}"
TS="$(date +%Y%m%d-%H%M%S)"
FILE="${BACKUP_DIR}/compass-${TS}.sql.gz.enc"

if [ -z "${CC_BACKUP_KEY:-}" ]; then
  echo "[backup] ОШИБКА: CC_BACKUP_KEY не задан." >&2
  echo "[backup] Резервная копия содержит спецкатегорию ПДн — шифрование обязательно" >&2
  echo "[backup] (152-ФЗ ст.10, ПП РФ №1119 УЗ-2). Задайте ключ >=32 байт." >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"

echo "[backup] pg_dump compass → ${FILE} (AES-256)"
docker compose -f "${COMPOSE}" exec -T postgres \
  pg_dump -U compass -d compass --no-owner --no-acl \
  | gzip -9 \
  | openssl enc -aes-256-cbc -pbkdf2 -iter 100000 -salt \
      -pass env:CC_BACKUP_KEY -out "${FILE}"

# retention: храним последние 30 дней
find "${BACKUP_DIR}" -name "compass-*.sql.gz.enc" -mtime +30 -delete 2>/dev/null || true

SIZE="$(du -h "${FILE}" | cut -f1)"
echo "[backup] done: ${FILE} (${SIZE}, AES-256)"
echo "[backup] retention: последние 30 дней (старые удалены)"
