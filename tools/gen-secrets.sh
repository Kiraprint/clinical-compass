#!/bin/sh
# F3 (ФСТЭК №21 ИАФ): генерация реальных секретов для prod/не-демо деплоя.
# Создаёт .env (gitignored) со случайными значениями + запрещает демо-секреты.
# Usage: tools/gen-secrets.sh infra/.env   (затем: docker compose -f infra/docker-compose.yml up -d)
set -eu

OUT="${1:-.env}"
if [ -f "${OUT}" ]; then
  echo "[gen-secrets] ${OUT} уже существует — не перезаписываю" >&2
  exit 1
fi

rand() { openssl rand -base64 "$1" | tr -d '\n'; }

cat > "${OUT}" <<EOF
# Сгенерировано tools/gen-secrets.sh — НЕ коммитить (.gitignore).
POSTGRES_PASSWORD=$(rand 24)
CC_JWT_SECRET=$(rand 48)
CC_DATA_KEY=$(rand 48)
CC_BACKUP_KEY=$(rand 48)
# Общий токен api <-> ML-сайдкар (X-CC-ML-Token); без него ml не стартует.
CC_ML_TOKEN=$(rand 32)
# prod: запрет демо-секретов (fail-fast), HTTPS cookie, TLS terminator
CC_ALLOW_DEMO_SECRETS=false
CC_COOKIE_SECURE=true
EOF

chmod 600 "${OUT}"
echo "[gen-secrets] ${OUT} создан (chmod 600). НЕ коммитить."
echo "[gen-secrets] Внимание: CC_ALLOW_DEMO_SECRETS=false — демо-база пуста, нужен реальный seed/миграция."
