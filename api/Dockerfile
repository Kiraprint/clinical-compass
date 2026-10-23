FROM python:3.12-slim
WORKDIR /app
# F8 (ГОСТ 56939): воспроизводимая сборка через uv.lock (полное разрешение
# зависимостей, не snapshot pip freeze). Слой зависимостей кешируется отдельно.
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml /app/core/pyproject.toml
COPY api/pyproject.toml /app/api/pyproject.toml
# F8 layer caching: первый sync — только сторонние зависимости всех workspace-членов
# (--all-packages нужен т.к. рут-проект не имеет deps, deps в членах core/api),
# без установки самих пакетов (--no-install-workspace) → кеш при change кода.
RUN uv sync --frozen --no-dev --all-packages --no-install-workspace
COPY core/ /app/core/
COPY api/ /app/api/
RUN uv sync --frozen --no-dev --all-packages
WORKDIR /app/api
EXPOSE 8000
# WEB_CONCURRENCY — число ПРОЦЕССОВ uvicorn (не потоков): каждый держит свой
# пул БД (CC_DB_POOL_SIZE + CC_DB_MAX_OVERFLOW) и свой in-process rate limiter
# логина. При дефолтах 20+20 бюджет max_connections=100 выдерживает 2 воркера;
# app.db.validate_pool_budget() падает на старте, если суммарный пул не влезает.
# Rate limit логина становится per-worker: при N воркерах окно CC_RATE_WINDOW
# допускает до N×CC_RATE_MAX попыток на процесс-аггрегат (см. README).
# F6: --proxy-headers → uvicorn читает X-Forwarded-For/X-Real-IP от nginx,
# request.client.host = реальный IP клиента (для audit_log.ip).
ENV WEB_CONCURRENCY=1
CMD ["/bin/sh", "-c", "exec /app/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --workers ${WEB_CONCURRENCY:-1}"]
