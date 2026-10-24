# Отчёт: ML-сайдкар на GPU (docs/08 §4.1)

Дата: 2026-10-24. Ветка `feat/ml-sidecar` (worktree `.worktrees/ml-sidecar`),
база — main `9d41f22`. Артефакты: `ml/` (сервис), `ml/Dockerfile`,
`infra/docker-compose.yml` (профиль `gpu`), `infra/.env.example`, документация
(docs/02, 03, 06, 08, 10, 12), README.

## Стенд

| | |
|---|---|
| GPU | NVIDIA GeForce RTX 5090 Laptop, 24 463 MiB |
| Драйвер | 615.78.08 (CUDA 12.8) |
| Python | 3.12.13 (`bench/.venv`) |
| torch | 2.11.0+cu128, bf16 на CUDA |
| sentence-transformers / transformers | 6.1.0 / 5.20.1 |
| Режим | `CC_ML_DEVICE=cuda`, реальные веса из локального HF-кеша |

Запуск: `PYTHONPATH=ml:core CC_ML_DEVICE=cuda uvicorn app:app --port <free>`;
проба — Python-клиент по HTTP (urllib) с `X-CC-ML-Token`, латентность
измерялась на стороне клиента (включая HTTP), 3 прогревочных вызова перед
серией. Скрипт пробы временный, в репозиторий не входит.

## Холодная загрузка и VRAM

| Метрика | Значение |
|---|---|
| Холодный старт до `status: ready` (веса в HF-кеше, `HF_HUB_OFFLINE=1`) | **19.4 с** |
| VRAM до старта (с другими процессами на карте) | 4 772 MiB |
| VRAM после загрузки всех трёх моделей | 8 910 MiB |
| Прирост на сайдкар | **≈ 4 138 MiB (4.0 GiB)** |

Веса (~4 ГБ) скачивает одноразовый `ml-warmup` (единственный контейнер с
egress); после него `ml` живёт в внутренней сети `mlnet` и стартует с
`HF_HUB_OFFLINE=1`, без обращений в сеть.

## Латентность (реальные модели, CUDA, bf16)

| Сценарий | n | p50, мс | p95, мс |
|---|---|---|---|
| `POST /v1/embeddings`, batch 1 (`query`, qwen3-0.6b) | 30 | **38.8** | **57.7** |
| `POST /v1/embeddings`, batch 8 (`passage`, qwen3-0.6b) | 25 | **44.8** | **47.4** |
| `POST /v1/rerank`, 20 документов (bge-reranker-v2-m3) | 25 | **66.8** | **71.1** |
| `POST /v1/mask`, 393 символа (rubert NER + regex) | 20 | **24.9** | **27.2** |

Контракт ответов проверен на тех же вызовах:

- embeddings: `profile="qwen3-0.6b"`, `dense_size=1024`, вектор длины 1024,
  L2-норма ≈ 1.0 (`sum(x²)=1.0036` — bf16, допустимо);
- rerank: 20 скоров, порядок = порядок `documents`, релевантный документ
  («серотониновый синдром…») получил **0.985** против ≤0.006 у остальных;
- health: `{status: "ready", device: "cuda", models: {embeddings: {default:
  qwen3-0.6b, loaded: [qwen3-0.6b], available: [e5-large-instruct, qwen3-0.6b]},
  rerank: BAAI/bge-reranker-v2-m3, mask: redmadrobot-rnd/rubert-base-pii-ner}}`.

### Второй профиль эмбеддингов

`e5-large-instruct` загружен лениво и проверен отдельным прогоном (в момент
повторной проверки карта была занята чужими процессами на 22.7 из 24 ГБ,
поэтому профиль запускался на CPU): `profile="e5-large-instruct"`,
`dense_size=1024`, длина вектора 1024, L2-норма 0.99985, `/health` показывает
оба загруженных профиля. GPU-латентность этого профиля не снималась —
профиль включается одной переменной `CC_ML_DEFAULT_PROFILE`.

## Маскирование ПДн: фактический ответ

Вход (393 символа):

```
Пациент: Иванов Иван Иванович, 45 лет. Дата рождения: 12.03.1980. Адрес:
г. Москва, ул. Ленина, д. 15, кв. 42. Телефон: +7 (916) 123-45-67. СНИЛС:
123-456-789 00. Жалобы: тревога, нарушения сна, снижение аппетита в течение
трёх недель. Ранее наблюдался у психотерапевта, принимал сертралин 50 мг
утром. Просит записать на повторный приём через две недели. Контакт для
связи: иванов@example.ru.
```

Ответ `POST /v1/mask` (`names: []`, NER сам нашёл ФИО):

```json
{
  "text": "Пациент: [ФИО] [ФИО] [ФИО], 45 лет. [ДАТА РОЖДЕНИЯ]. Адрес: [АДРЕС], [АДРЕС]. Телефон: [ТЕЛЕФОН]. СНИЛС: [ДОКУМЕНТ]. Жалобы: тревога, нарушения сна, снижение аппетита в течение трёх недель. Ранее наблюдался у психотерапевта, принимал сертралин 50 мг утром. Просит записать на повторный приём через две недели. Контакт для связи: [EMAIL].",
  "spans": [
    {"category": "FIO", "start": 9, "end": 15},
    {"category": "FIO", "start": 16, "end": 20},
    {"category": "FIO", "start": 21, "end": 29},
    {"category": "DOB", "start": 39, "end": 64},
    {"category": "ADDRESS", "start": 73, "end": 82},
    {"category": "ADDRESS", "start": 84, "end": 109},
    {"category": "PHONE", "start": 120, "end": 138},
    {"category": "DOC", "start": 147, "end": 161},
    {"category": "EMAIL", "start": 375, "end": 392}
  ],
  "model": "hybrid",
  "degraded": false
}
```

Спаны — координаты исходного текста, отсортированы по `start`. Клинический
смысл сохранён: жалобы, препарат и дозировка не тронуты.

**Совпадение с локальным гибридом.** Тот же текст прогнан через
`api/app/services/depersonalize.py::RubertPiiNameExtractor` +
`core/compass/depersonalize.py::depersonalize` (локальный
`CC_DEPERSONALIZE_NER=rubert`, CPU): NER-имена `['Иванов', 'Иван',
'Иванович']`, итоговый текст и список спанов **побайтово совпали** с ответом
сайдкара. Разбиение ФИО на три спана — поведение самого NER (по слову на
сущность) и локального backend'а, не расхождение реализаций.

## Сборка и конфигурация

| Проверка | Результат |
|---|---|
| `docker compose -f infra/docker-compose.yml config` | exit 0 |
| `docker compose -f infra/docker-compose.yml --profile gpu config` | exit 0, сервис `ml` + GPU-reservation + том `ml-models` |
| `docker build -f ml/Dockerfile -t clinical-compass-ml:local .` | собрался, 6.3 мин |
| Размер образа `clinical-compass-ml:local` | **12.0 GB** (torch cu128 + CUDA-библиотеки; модели в образ не входят) |

Dockerfile проверен сборкой, но контейнер на GPU в этом прогоне не
запускался: порт у сервиса внутренний, проверка эндпоинтов выполнена
uvicorn'ом на хосте с теми же зависимостями. GPU-reservation в compose
соответствует проверенному на хосте `docker run --gpus all`.

## Тесты

| Команда | Результат |
|---|---|
| `python -m pytest ml/tests -q` | **30 passed** (stub, без torch/GPU/Postgres) |
| `PYTHONPATH=api:core … pytest api/tests/test_depersonalize_remote.py -q` | **22 passed** |
| `PYTHONPATH=api:core … pytest api/tests/test_ml_client.py -q` | **9 passed** |

API-тесты запускались с `CC_TEST_DATABASE_URL=…/compass_pii`,
`CC_QDRANT_COLLECTION=kb_pii`, `CC_ALLOW_DEMO_SECRETS=true`. Первый прогон
дал 8 passed + 1 fixture error (`drugs_drug_id` duplicate) из-за параллельного
полного прогона `api/tests` другим процессом на той же БД; после его
завершения — 9 passed.

## Безопасность после ревью (P2-1…P2-3)

- **Токен fail-closed.** Без `CC_ML_TOKEN` приложение не стартует
  (`RuntimeError` при импорте): анонимный режим только через явный
  `CC_ML_ALLOW_ANONYMOUS=1` с WARNING в лог. `tools/gen-secrets.sh` теперь
  генерирует `CC_ML_TOKEN`, а `.env.example` больше не обещает выключенную
  проверку.
- **Сеть.** `mlnet` объявлена `internal: true`; в ней только api и ml.
  postgres/qdrant/redis остаются в default, у `ml` нет egress. Веса скачивает
  одноразовый `ml-warmup` (default-сеть) в том `ml-models`; после него `ml`
  работает с `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1` (проверено прогоном:
  `status=ready` при offline-переменных).
- **Честный NER.** Длинный текст режется на окна 1200/200 с абсолютными
  смещениями; исключение в любом окне → `_broken=True`,
  `model="regex"`, `degraded=true`. Api (`_spans_from_remote`) считает
  `degraded=true` или небулев `degraded` отказом backend'а: в prod — 503
  `DEPERSONALIZATION_UNAVAILABLE`, вне prod — regex-деградация с WARNING.
- **Тело и утечки.** Тело больше лимита (1/6/24 МиБ) отсекается по
  `Content-Length` до парсинга (413 `payload_too_large`); `/health` отдаёт
  только `error: bool`, тексты 503 не содержат путей к весам/токена.

### Длинный текст: ФИО в конце 6000 символов

Прогон на реальных весах (`CC_ML_DEVICE=cuda`, `HF_HUB_OFFLINE=1`):
6000 символов клинической записи, ФИО «Смирнов Алексей Петрович» только в
последних 80 символах (позиция 5921). Ответ: `model=hybrid`,
`degraded=false`, 4 спана, `[ФИО]` на месте имени; телефон и дата рождения
замаскированы; `"Смирнов"` и `"14.07.1972"` в тексте ответа отсутствуют.
Первое обращение к длинному тексту — 2.6 с (прогрев пайплайна), далее
p50 **130 мс** / p95 **143 мс** (n=10). До фикса текст обрезался первым
512-токенным окном rubert и ФИО в хвосте не находилось.

## Что осталось для боевого переключения

1. Собрать образ на целевом хосте, прогреть том
   (`tools/gen-secrets.sh` → `docker compose --profile gpu run --rm ml-warmup`)
   и поднять `ml`; дальше сервис работает офлайн.
2. Убедиться, что api видит `ml` по `http://ml:8080` и общий `CC_ML_TOKEN`
   совпадает (иначе 401/403); включить `CC_DEPERSONALIZE_NER=remote`.
3. Для эмбеддингов выбрать `CC_ML_DEFAULT_PROFILE` по замерам retrieval-агента
   и переиндексировать коллекцию (векторы разных моделей несравнимы).
4. Для не-GPU окружений ничего не меняется: профиль `gpu` не поднимается,
   `CC_EMBEDDER=hash`, `CC_DEPERSONALIZE_NER=off`, `CC_ML_URL` пустой.
