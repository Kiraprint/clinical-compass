# ML-сайдкар Clinical Compass

GPU-сервис с тремя моделями, которые нельзя держать в образе `api` (torch и
веса — гигабайты, прод-api обязан оставаться torch-free):

| Эндпоинт | Модель | Назначение |
|---|---|---|
| `POST /v1/embeddings` | `Qwen/Qwen3-Embedding-0.6B` (профиль по умолчанию) или `intfloat/multilingual-e5-large-instruct` | dense-векторы БЗ, 1024; асимметричные префиксы query/passage |
| `POST /v1/rerank` | `BAAI/bge-reranker-v2-m3` (CrossEncoder) | cross-encoder реранк, сырые логиты |
| `POST /v1/mask` | `compass.depersonalize` + `redmadrobot-rnd/rubert-base-pii-ner` | маскирование ПДн, `model: "hybrid"` |
| `GET /health` | — | `status` (`loading`/`ready`/`error`), `device`, `models` |

Контракт заморожен в `api/app/services/ml_client.py`; конфигурация и
обоснование — `docs/08 §4`, маскирование — `docs/12`, граница доверия —
`docs/10`.

## Установка и запуск

```bash
# из корня репозитория
uv pip install -r ml/requirements.txt        # + torch cu128 отдельно:
# pip install --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0
PYTHONPATH=ml:core CC_ML_DEVICE=cuda CC_ML_TOKEN=dev \
  uvicorn app:app --host 0.0.0.0 --port 8080
```

Контейнер (профиль `gpu`, видеокарта через `deploy.resources.reservations`):

```bash
docker compose -f infra/docker-compose.yml --profile gpu up -d ml
docker compose -f infra/docker-compose.yml --profile gpu logs -f ml
docker compose -f infra/docker-compose.yml --profile gpu exec ml python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/health')))"
```

Порт на хост не публикуется: `ml` доступен только из сети compose
(`http://ml:8080` для `api`). Первый старт скачивает веса в том `ml-models`
(`HF_HOME=/models`, ~4 ГБ) — разовый egress; после этого сервис работает
офлайн.

## Переменные окружения

| Переменная | Дефолт | Смысл |
|---|---|---|
| `CC_ML_STUB` | `0` | `1` — детерминированные заглушки без torch (тесты/CI) |
| `CC_ML_DEVICE` | `auto` | `auto` \| `cuda` \| `cpu`; CUDA — bf16, CPU — fp32 |
| `CC_ML_TOKEN` | пусто | общий секрет, заголовок `X-CC-ML-Token` (401 без, 403 неверный) |
| `CC_ML_DEFAULT_PROFILE` | `qwen3-0.6b` | профиль эмбеддингов по умолчанию (одна переменная — смена без кода) |
| `CC_ML_PROFILES` | пусто | JSON-переопределение/добавление профилей |
| `CC_ML_QUERY_INSTRUCTION` | английская клиническая инструкция | подставляется в `{instruction}` префикса запроса |
| `CC_ML_RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | id cross-encoder |
| `CC_ML_NER_MODEL` | `redmadrobot-rnd/rubert-base-pii-ner` | id token-classifier |
| `HF_HOME` | `/models` (образ) | кеш весов (том `ml-models`) |

Профиль в `CC_ML_PROFILES`:

```json
{"my-tune": {"model": "org/model", "dense_size": 1024,
             "query_prefix": "Instruct: {instruction}\nQuery: ",
             "passage_prefix": ""}}
```

Пороги входа — в `ml/models.py` (`MAX_TEXTS=64`, `MAX_DOCUMENTS=256`,
`MAX_CHARS=20000`, `MAX_NAMES=256`); превышение — 422 `invalid_request`.

## Модели и память

Замер на RTX 5090 Laptop 24 ГБ (bf16, все три модели загружены):
VRAM +4.1 ГБ, холодная загрузка из локального HF-кеша ≈ 20 с (в контейнере —
плюс скачивание при первом старте). Латентности и точная таблица —
`REPORT-ml-sidecar.md` в корне репозитория.

## Тесты

```bash
python -m pytest ml/tests -q            # stub, без torch/GPU/сети
```

Проверяются формы контракта, токен (401/403), 503 во время загрузки, лимиты,
порядок и границы спанов, выравнивание реранка и размерность векторов.

## Диагностика

| Симптом | Причина/что делать |
|---|---|
| эндпоинты 503 `model_loading`, `/health` `loading` | идёт загрузка/скачивание весов; смотрите `logs -f ml` |
| 503 `model_unavailable` | загрузка упала: в `/health.error` текст (нет весов, OOM) |
| `CUDA out of memory` | освободить VRAM (другие LLM на той же карте) или `CC_ML_DEVICE=cpu` |
| первый старт висит на скачивании | нет egress до Hugging Face: прогрейте том `ml-models` на машине с сетью |
| 401/403 от api | `CC_ML_TOKEN` не совпадает у `api` и `ml` |
| `device: cpu` при `CC_ML_DEVICE=cuda` | нет `nvidia-container-toolkit`/драйвера — проверьте `docker run --gpus all` |
