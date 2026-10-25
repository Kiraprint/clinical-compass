# ML-сайдкар Clinical Compass

GPU-сервис с тремя моделями, которые нельзя держать в образе `api` (torch и
веса — гигабайты, прод-api обязан оставаться torch-free):

| Эндпоинт | Модель | Назначение |
|---|---|---|
| `POST /v1/embeddings` | `Qwen/Qwen3-Embedding-0.6B` (профиль по умолчанию) или `intfloat/multilingual-e5-large-instruct` | dense-векторы БЗ, 1024; асимметричные префиксы query/passage |
| `POST /v1/rerank` | `BAAI/bge-reranker-v2-m3` (CrossEncoder) | cross-encoder реранк, сырые логиты |
| `POST /v1/mask` | `compass.depersonalize` + `redmadrobot-rnd/rubert-base-pii-ner` | маскирование ПДн; `model: hybrid\|regex`, `degraded: bool` |
| `GET /health` | — | `status` (`loading`/`ready`/`error`), `device`, `models`, `error: bool` |

Контракт заморожен в `api/app/services/ml_client.py`; конфигурация и
обоснование — `docs/08 §4.1`, маскирование — `docs/12`, граница доверия —
`docs/10 §3.1`.

## Установка и запуск

```bash
# из корня репозитория
uv pip install -r ml/requirements.txt        # + torch cu128 отдельно:
# pip install --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0
PYTHONPATH=ml:core CC_ML_DEVICE=cuda CC_ML_TOKEN=dev \
  uvicorn app:app --host 0.0.0.0 --port 8080
```

`CC_ML_TOKEN` обязателен: без него приложение не стартует (fail-closed).
Для локальной отладки без токена есть явный `CC_ML_ALLOW_ANONYMOUS=1`.

Контейнер (профиль `gpu`, видеокарта через `deploy.resources.reservations`):

```bash
tools/gen-secrets.sh infra/.env                        # создаёт CC_ML_TOKEN
docker compose -f infra/docker-compose.yml --profile gpu run --rm ml-warmup
docker compose -f infra/docker-compose.yml --profile gpu up -d ml
docker compose -f infra/docker-compose.yml --profile gpu logs -f ml
docker compose -f infra/docker-compose.yml --profile gpu exec ml python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/health')))"
```

Порт на хост не публикуется: `ml` доступен только из сети compose
(`http://ml:8080` для `api`). Сеть `mlnet` объявлена `internal: true` — у
`ml` нет egress и он не видит postgres/qdrant/redis; api подключён к `mlnet`
вторым интерфейсом. Веса (~4 ГБ) скачивает одноразовый `ml-warmup`
(default-сеть, единственный шаг с egress), после чего `ml` стартует с
`HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1`. Пустой том при offline-старте —
ошибка загрузки.

## Переменные окружения

| Переменная | Дефолт | Смысл |
|---|---|---|
| `CC_ML_STUB` | `0` | `1` — детерминированные заглушки без torch (тесты/CI) |
| `CC_ML_DEVICE` | `auto` | `auto` \| `cuda` \| `cpu`; `auto` — CUDA при доступной карте |
| `CC_ML_TOKEN` | — | общий секрет `X-CC-ML-Token`; **обязателен**, иначе старт падает |
| `CC_ML_ALLOW_ANONYMOUS` | `0` | `1` — разрешить пустой токен (только локальная отладка) |
| `CC_ML_DEFAULT_PROFILE` | `qwen3-0.6b` | профиль эмбеддингов по умолчанию (`qwen3-0.6b`, `e5-large-instruct`, `bge-m3`) |
| `CC_ML_PROFILES` | — | JSON `{имя: {model, dense_size, query_prefix, passage_prefix}}` |
| `CC_ML_QUERY_INSTRUCTION` | англ. инструкция | текст `Instruct:` для instruct-эмбеддеров |
| `CC_ML_RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | cross-encoder |
| `CC_ML_NER_MODEL` | `redmadrobot-rnd/rubert-base-pii-ner` | token-classifier для ФИО/адресов |
| `HF_HOME` | `/models` | кеш весов (том `ml-models`) |
| `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` | `1` в compose | запрет сетевых обращений к HF |

Пример `CC_ML_PROFILES`:

```json
{"qwen3-0.6b": {"model": "Qwen/Qwen3-Embedding-0.6B", "dense_size": 1024},
 "custom": {"model": "org/model", "dense_size": 768, "query_prefix": "", "passage_prefix": ""}}
```

## Ограничения входа

Пороги — в `ml/models.py` (`MAX_TEXTS=64`, `MAX_DOCUMENTS=256`,
`MAX_CHARS=20000`, `MAX_NAMES=256`); превышение — 422 `invalid_request`.
Дополнительно тело запроса отсекается по `Content-Length` до парсинга JSON:
mask — 1 МиБ, embeddings — 6 МиБ, rerank — 24 МиБ (413 `payload_too_large`).

Длинный текст для NER режется на окна по 1200 символов с перекрытием 200
(`NER_CHUNK_CHARS`/`NER_CHUNK_OVERLAP`), поэтому ФИО в конце документа
находится, а не отбрасывается 512-токенным лимитом rubert. При сбое NER
ответ честный: `model="regex"`, `degraded=true` (api в prod считает это
отказом backend'а, docs/12).

## Модели и память

Замер на RTX 5090 Laptop 24 ГБ (bf16, все три модели загружены):
VRAM +4.1 ГБ, холодная загрузка из локального HF-кеша ≈ 20 с (в контейнере
после `ml-warmup` — без скачивания). Латентности и точная таблица —
`REPORT-ml-sidecar.md` в корне репозитория.

## Тесты

```bash
python -m pytest ml/tests -q            # stub, без torch/GPU/сети/токена
```

Проверяются формы контракта, обязательный токен и анонимный режим только по
флагу, 503 во время загрузки, лимиты и отсечение тела, порядок и границы
спанов, выравнивание реранка, размерность векторов, окна NER и деградация
`degraded=true`.

## Диагностика

| Симптом | Причина/что делать |
|---|---|
| старт падает `CC_ML_TOKEN не задан` | сгенерируйте `.env` (`tools/gen-secrets.sh`) или задайте токен |
| `RuntimeError: ... не загрузились` / `model_unavailable` | смотрите `logs -f ml`: нет весов (запустите `ml-warmup`) или OOM |
| `CUDA out of memory` | освободить VRAM (другие LLM на карте) или `CC_ML_DEVICE=cpu` |
| `ml` не видит интернет | так и задумано (`mlnet` внутренняя); качайте веса через `ml-warmup` |
| 401/403 от api | `CC_ML_TOKEN` не совпадает у `api` и `ml` |
| `device: cpu` при `CC_ML_DEVICE=cuda` | нет `nvidia-container-toolkit`/драйвера — проверьте `docker run --gpus all` |
