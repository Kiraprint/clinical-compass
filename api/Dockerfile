FROM python:3.12-slim
WORKDIR /app
# F8 (ГОСТ 56939): воспроизводимая сборка через uv.lock (полное разрешение
# зависимостей, не snapshot pip freeze). Слой зависимостей кешируется отдельно.
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml /app/core/pyproject.toml
COPY api/pyproject.toml /app/api/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-project
COPY core/ /app/core/
COPY api/ /app/api/
RUN uv sync --frozen --no-dev
WORKDIR /app/api
EXPOSE 8000
# venv-бинарь напрямую (uv run ре-синкает на каждом старте — лишние ~10с и сеть).
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
