#!/bin/sh
# F7 (ФСТЭК №21; ПП РФ №1119): резервное копирование БД clinical-compass.
# pg_dump → gzip → tools/backups/compass-YYYYMMDD-HHMMSS.sql.gz
# Cron: 0 3 * * * /home/kir/Code/clinical-compass/tools/backup.sh
# Восстановление: tools/restore.sh <file.sql.gz>
set -eu

COMPOSE="${COMPOSE:-infra/docker-compose.yml}"
BACKUP_DIR="${BACKUP_DIR:-tools/backups}"
TS="$(date +%Y%m%d-%H%M%S)"
FILE="${BACKUP_DIR}/compass-${TS}.sql.gz"

mkdir -p "${BACKUP_DIR}"

echo "[backup] pg_dump compass → ${FILE}"
docker compose -f "${COMPOSE}" exec -T postgres \
  pg_dump -U compass -d compass --no-owner --no-acl \
  | gzip -9 > "${FILE}"

# retention: храним последние 30 дней
find "${BACKUP_DIR}" -name "compass-*.sql.gz" -mtime +30 -delete 2>/dev/null || true

SIZE="$(du -h "${FILE}" | cut -f1)"
echo "[backup] done: ${FILE} (${SIZE})"
echo "[backup] retention: последние 30 дней (старые удалены)"
