# Нагрузочные и ML-замеры Clinical Compass

Изолированный стенд для ответа на вопросы «сколько выдерживает» и «насколько
хорошо ищет». Прод/демо-стек не затрагивается: своя БД `compass_loadtest`, своя
коллекция Qdrant `kb_loadtest`, отдельный процесс uvicorn на `127.0.0.1:8011`.

## Что измеряется

1. **HTTP-сервис** (`run_http.py`): замкнутый контур по уровням конкуренции
   (1–48 воркеров, 8 с на уровень). Для каждого уровня: RPS, p50/p95/p99,
   ошибки, RSS и CPU процесса. `max RPS` — лучший уровень с ошибками < 1 %.
   Сценарии: `/health` (предел HTTP-слоя), чтение/запись дневника, чек-ин,
   полный скрининг (3 запроса), `POST /assistant/ask`, роспись врача и
   смешанный профиль пациент+врач.
2. **ML-метрики поиска** (`run_search.py`): 55 золотых запросов RU+EN с qrels
   из `bench/data`. Два среза — прямой `hybrid_search` (Qdrant RRF) и сквозной
   путь агента `POST /assistant/ask` (relevance gate + цитаты + аудит).
   Recall@1/3/5/10, nDCG@3/10, MRR@3/10, Hit@3, доля grounded/refused, red-line.
3. **Ядро** (`run_core.py`): чистые функции скоринга, триажа, маршрутизации,
   чек-инов, деперсонализации, RAG-сборки, hash-эмбеддера и `build_corpus`
   (выполняется на каждый запрос агента). ops/s, p50/p95, масштабирование
   по процессам.
4. **Предел пула** (`run_http.run_cliff`): свежий процесс API на каждую точку,
   C=56–64 на дефолтном пуле (5+10) и C=64–128 на пуле 80+20 — фиксирует обрыв
   и проверяет, что он снимается размером пула.

Результаты: `loadtest/results/*.json`, сводка — `loadtest/REPORT.md`.

## Предварительные условия

- Postgres и Qdrant демо-стека: `docker compose -f infra/docker-compose.yml up -d postgres qdrant`
  (порты `127.0.0.1:5432` и `127.0.0.1:6333`).
- Python 3.12 с зависимостями API. Прод-образ (`api/Dockerfile`) — python:3.12-slim,
  поэтому замеры гоняем на 3.12, а не на 3.14 из корневого `.venv`:

  ```bash
  export UV_PROJECT_ENVIRONMENT=/tmp/cc312
  uv venv --python 3.12 /tmp/cc312
  uv sync --frozen --all-packages --no-dev
  /tmp/cc312/bin/python -m loadtest.run_all
  ```

## Запуск

```bash
cd .worktrees/load-tests

# полный прогон: provision → API → ML → HTTP → ядро → отчёт
/home/kir/Code/clinical-compass/.venv/bin/python -m loadtest.run_all

# быстрый дым (3 уровня конкуренции, 5 с)
/home/kir/Code/clinical-compass/.venv/bin/python -m loadtest.run_all --quick

# по частям
python -m loadtest.provision      # пересоздать БД/индекс
python -m loadtest.run_http       # только HTTP (нужен запущенный API)
python -m loadtest.run_search     # ML-метрики (нужен запущенный API)
python -m loadtest.run_core       # только ядро
python -m loadtest.report         # пересобрать REPORT.md из results/*.json
```

Запускать интерпретатором, в котором установлены зависимости API: процессы
провижина и сервера используют `sys.executable`.

## Ограничения методики

- Один процесс uvicorn (как в демо/прод-конфигурации), без nginx и TLS.
- Postgres и Qdrant — локально в Docker на том же хосте; сеть между сервисами
  не моделируется.
- Эмбеддер по умолчанию — `hash` (демо/офлайн). `--embedder bge-m3` сработает
  только если `torch`/`sentence-transformers` есть в интерпретаторе API; для
  прод-качества используйте цифры `bench/run_bench.py`.
- LLM отключён: измеряется детерминированный RAG-путь. Реальный LLM добавляет
  секунды к p95 и требует отдельного стенда.
- Замкнутый контур: латентность включает очередь к пулу БД, поэтому на
  насыщении p95 растёт вместе с конкуренцией — это ожидаемо.
- `max RPS` — не «предел железа», а точка, до которой стенд работал с ошибками
  < 1 %; выше неё смотрите кривые и долю ошибок.
