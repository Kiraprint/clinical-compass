# Нагрузочные и ML-замеры Clinical Compass

Изолированный стенд для ответа на вопросы «сколько выдерживает» и «насколько
хорошо ищет». Прод/демо-стек не затрагивается: своя БД `compass_loadtest`, своя
коллекция Qdrant `kb_loadtest`, отдельный процесс uvicorn на `127.0.0.1:8011`.

## Предохранитель: что стенд имеет право трогать

`provision` делает `DROP DATABASE ... WITH (FORCE)` и пересоздаёт коллекцию
Qdrant. Поэтому:

- имя БД обязано начинаться с `compass_load` (по умолчанию `compass_loadtest`);
- имя коллекции — с `kb_load` (по умолчанию `kb_loadtest`);
- разрушающие операции выполняются только при `CC_LOADTEST_ALLOW=1`;
- проверка идёт **до** любого соединения, отказ называет offending-значение и
  завершает процесс с кодом 2.

```bash
# только показать цели, не подключаясь:
python -m loadtest.provision --dry-run
# проверить, что стенд откажется от чужой БД:
python -m loadtest.provision --db compass --dry-run   # exit 2
```

Имена резолвятся в порядке «явный CLI-аргумент → переменная окружения →
дефолт», поэтому `--db`/`--collection` никогда не затираются дефолтами
`common.setup_env()`. Переопределения: `CC_LOADTEST_DB`,
`CC_LOADTEST_COLLECTION`, `CC_QDRANT_URL`, `CC_LOADTEST_HOST/PORT`,
`CC_LOADTEST_PYTHON` (интерпретатор процесса API), `CC_LOADTEST_BENCH_DATA`.

Параллельные прогоны на одной машине не должны делить БД/коллекцию/порт:

| Что | Переменная | По умолчанию |
|---|---|---|
| БД стенда | `CC_LOADTEST_DB` (allowlist `compass_load*`) | `compass_loadtest` |
| Полный URL БД | `CC_LOADTEST_DATABASE_URL` | собирается из `CC_LOADTEST_PG_*` |
| Postgres | `CC_LOADTEST_PG_HOST` / `_PORT` / `_USER` / `_PASSWORD` | `127.0.0.1:5432`, `compass/compass` |
| Коллекция Qdrant | `CC_LOADTEST_COLLECTION` (allowlist `kb_load*`) | `kb_loadtest` |
| Qdrant | `CC_QDRANT_URL` | `http://127.0.0.1:6333` |
| Порт API | `CC_LOADTEST_PORT` | `8011` |
| Потолок соединений | `CC_DB_MAX_CONNECTIONS` | `100` |

`CC_LOADTEST_DATABASE_URL` приоритетнее сборки из частей; имя БД из него всё
равно проходит allowlist, поэтому «чужая» БД отвергается до соединения.
Пример изолированного прогона рядом с чужим стендом:

```bash
CC_LOADTEST_DB=compass_load_mw CC_LOADTEST_COLLECTION=kb_load_mw \
CC_LOADTEST_PORT=8011 python -m loadtest.run_workers
```

## Что измеряется

1. **HTTP-сервис** (`run_http.py`): замкнутый контур по уровням конкуренции
   (1–48 воркеров, 8 с на уровень). Для каждого уровня: RPS, p50/p95/p99,
   число запросов, ошибки, RSS и CPU процесса. `max RPS` — лучший уровень
   с ошибками < 1 %. Сценарии: `/health` (предел HTTP-слоя), чтение/запись
   дневника, чек-ин, полный скрининг (3 запроса), `POST /assistant/ask`,
   роспись врача и смешанный профиль пациент+врач.
2. **ML-метрики поиска** (`run_search.py`): золотой набор RU+EN с qrels из
   `bench/data`. Два среза — прямой `hybrid_search` (Qdrant RRF) и сквозной
   путь агента `POST /assistant/ask` (relevance gate + цитаты + аудит).
   Recall@1/3/5/10, nDCG@3/10, MRR@3/10, Hit@3, доля grounded/refused,
   red-line.
3. **Ядро** (`run_core.py`): чистые функции скоринга, триажа, маршрутизации,
   чек-инов, деперсонализации, RAG-сборки, hash-эмбеддера и `build_corpus`
   по TTL-кешу (это попадание в снимок, а **не** сборка на запрос агента).
   Плюс реальный путь агента in-process: `AgentFactory.invoke` → гибридный
   поиск Qdrant → `build_rag_answer` (LLM выключен). Если коллекции нет,
   замер помечается `skipped` с причиной.
4. **Предел пула** (`run_http.run_cliff`): свежий процесс API на каждую точку,
   C=56–128 на async-пуле по умолчанию (20+20) и C=64/96 на увеличенном пуле
   60+15 = 75 соединений. Больший пул стартовать не может: `validate_pool_budget`
   требует `workers × (pool + overflow + 2) ≤ max_connections` (100 по умолчанию),
   поэтому прежняя точка 80+20 недостижима и не является целевым режимом.

5. **Многопроцессный режим** (`run_workers.py`): `WEB_CONCURRENCY` = 1, 2, 4
   (свежий сервер на каждое число воркеров), сценарии `health` и `diary_read`,
   C=16/32/48/64/96, 8 с на точку, прогрев 0.75 с. Пишутся RPS, p50/p95/p99,
   типы и число ошибок, CPU дерева процессов (supervisor + все воркеры), пул
   на процесс, снимок `pg_stat_activity` после каждой точки и контроль дрейфа
   машины (те же 1 воркер и C=32 в начале и в конце прогона). Результат —
   `loadtest/results/workers.json`, раздел «Многопроцессный режим» в отчёте.

   ```bash
   python -m loadtest.run_workers --workers 1,2,4 --concurrency 16,32,48,64,96
   python -m loadtest.report     # пересобрать REPORT.md, если нужен только этот раздел
   ```

### Бюджет соединений Postgres

Каждый процесс uvicorn держит свой пул, поэтому суммарный спрос растёт линейно
по воркерам. `app.db.validate_pool_budget()` падает на старте воркера, если:

```
N × (CC_DB_POOL_SIZE + CC_DB_MAX_OVERFLOW + 2 служебных)
  + фон × (CC_DB_BG_POOL_SIZE + CC_DB_BG_MAX_OVERFLOW + 2)
  ≤ CC_DB_MAX_CONNECTIONS
```

В стенде фоновые процессы не запущены (`CC_DB_BACKGROUND_PROCESSES=0`), поэтому
бюджет равен `N × (pool + overflow + 2)`. Наборы `run_workers.py` при
`CC_DB_MAX_CONNECTIONS=100`:

| Воркеров | Пул на процесс | Бюджет |
|---:|---:|---:|
| 1 | 20 + 20 | 42 |
| 2 | 20 + 20 | 84 |
| 4 | 8 + 8 | 72 |

Пул уменьшается вместе с ростом числа воркеров не «для экономии», а потому что
иначе `validate_pool_budget` не пустит процесс. Ошибка конфигурации попадает в
`workers.json` (поле `error`) и в отчёт текстом, а не превращается в «сервер не
поднялся».

Результаты: `loadtest/results/*.json` (в git не коммитятся), сводка —
`loadtest/REPORT.md` (генерируется `report.py`, коммитится).

## Предварительные условия

- Postgres и Qdrant демо-стека: `docker compose -f infra/docker-compose.yml up -d postgres qdrant`
  (порты `127.0.0.1:5432` и `127.0.0.1:6333`).
- Python 3.12 с зависимостями API (прод-образ `api/Dockerfile` — python:3.12-slim).
  Асинхронный рантайм требует `greenlet`: без него `import app.db` падает с
  `ImportError: SQLAlchemy asyncio module requires greenlet`. Ставьте
  `uv pip install --python <venv>/bin/python greenlet` (или `uv sync`),
  иначе provision упадёт на `init_schema`.
- `bench/data/{queries,corpus}.jsonl`: `bench/data` не коммитится, соберите
  набор или укажите `CC_LOADTEST_BENCH_DATA=/path/to/bench/data`.

## Запуск

```bash
export CC_LOADTEST_ALLOW=1
python -m loadtest.provision --dry-run     # цели и allowlist
python -m loadtest.provision               # схема + users + drugs + Qdrant
python -m loadtest.run_all                 # всё: provision → API → ML → HTTP → ядро → REPORT.md
python -m loadtest.run_all --quick         # короткий смоук
```

Отдельные стадии (нужен уже провиженный стенд и запущенный API на 8011):

```bash
python -m loadtest.run_core
python -m loadtest.run_search
python -m loadtest.run_http
python -m loadtest.report                  # пересобрать REPORT.md из results/
```

У каждой стадии есть `--db`/`--collection`/`--embedder`; у `run_http` —
`--quick`. Порядок важен: `run_search`/`run_http`/`run_core.agent_path`
читают Qdrant, поэтому без `provision` они упадут или пропустят замер.

## Provision: что создаётся

- пустая БД (allowlist), схема `db.init_db()`;
- 256 пациентов, 8 врачей, согласия, дневник, роспись;
- справочник препаратов (`app.seed._seed_drugs` ← `api/app/data/drugs.json`):
  без него KB-чанки препаратов не строятся;
- коллекция Qdrant: корпус приложения (включая drug-чанки) + gold-корпус
  `bench/data/corpus.jsonl`.

`provision.json` в `results/` фиксирует имена БД/коллекции, эмбеддер и
устройство, число чанков/документов/препаратов/пользователей.

## Провенанс и воспроизводимость

`report.py` пишет раздел 0: git-коммит и признак dirty-дерева, дату прогона в
MSK, эмбеддер и device, число чанков/документов/препаратов/пользователей,
Python, CPU/RAM, состояние LLM, а также число сэмплов у каждой метрики.
Раздел «Как перемерить» в отчёте содержит точные команды для этого коммита.

`loadtest/results/` в `.gitignore`: JSON-артефакты живут только локально,
поэтому сравнивать прогоны можно лишь по отчётам с зафиксированным коммитом.
Старые числа в `REPORT.md` без коммита/сырых результатов не воспроизводимы —
они удалены, а не переписаны.
