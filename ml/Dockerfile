# ML-сайдкар (docs/08 §4): эмбеддинги, реранк, маскирование ПДн.
# Отдельный образ от api: torch cu128 и модели — десятки ГБ, в прод-api они
# не попадают (api остаётся torch-free). Сборка из корня репозитория:
#   docker compose -f infra/docker-compose.yml --profile gpu build ml
#   docker build -f ml/Dockerfile -t clinical-compass-ml .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PYTHONPATH=/app/core:/app

# libgomp — OpenMP для torch; больше ничего для CPU-пути не нужно.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch отдельным слоем: cu128-колёса тяжёлые и меняются реже кода.
RUN pip install --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0
COPY ml/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# core/ нужен целиком: compass.depersonalize — та же гибридная логика, что в api.
COPY core/ /app/core/
COPY ml/app.py ml/models.py ml/warmup.py /app/
COPY ml/README.md /app/ML-README.md

# Модели кешируются в томе ml-models (HF_HOME=/models), пользователь должен
# иметь право писать туда.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /models \
    && chown -R app:app /models /app
USER app

EXPOSE 8080
# Готовность — не просто HTTP 200, а status=ready: во время загрузки моделей
# /health отвечает 200 с status=loading, и compose не должен считать контейнер
# здоровым раньше, чем эндпоинты перестанут отдавать 503.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10m --retries=3 \
    CMD ["python", "-c", "import json,urllib.request,sys; d=json.load(urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)); sys.exit(0 if d.get('status')=='ready' else 1)"]

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
