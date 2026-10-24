# Бенчмарк деперсонализации (docs/12)

Два независимых замера:

1. **Клинический gold-корпус** (`clinical_contexts/`) — основная метрика: 332
   синтетических контекста ровно тех форм, что уходят в LLM (чат пациента,
   дневник/чек-ин, вопрос врача, батч FR-31), gold-спаны по конструкции.
2. **Публичный pii_benchmark** (`data/`, redmadrobot-rnd, MIT, 2841 строка,
   22 категории) — вторичная проверка сопоставимости с прошлыми замерами;
   текст не коммитится (`fetch_benchmark.py` → gitignored `data/`).

Кандидаты: структурные правила ядра (`regex`), rubert-base-pii-ner без правил
(`rubert`), GLiNER zero-shot (`gliner`), LLM-экстрактор через шлюз (`llm`),
прод-путь `hybrid` (правила + rubert + валидаторы).

## Клинический корпус (`clinical_contexts/`)

```bash
bench/.venv/bin/python bench/ner/clinical_contexts/build_corpus.py   # gold (коммитится)
bench/.venv/bin/python bench/ner/run_clinical_bench.py --predict regex,rubert,hybrid --save
/tmp/pii-gliner/bin/python bench/ner/run_clinical_bench.py --predict gliner --save
CC_ASSISTANT_LLM_URL=http://172.17.0.1:20133/v1 CC_ASSISTANT_LLM_API_KEY=... \
  bench/.venv/bin/python bench/ner/run_clinical_bench.py --predict llm --save --llm-batch 6
bench/.venv/bin/python bench/ner/run_clinical_bench.py --eval-only
```

Метрика: entity-level precision/recall по категориям; совпадение — пересечение
≥50% длины более короткого спана (маскирование посимвольное). Категории:
FIO, PHONE, EMAIL, ADDRESS, DOB, SNILS, INN, OMS, PASSPORT, MEDCARD, AGE
(возраст только в связке с ФИО; одиночный возраст — клинические данные).

Замер 2026-10-24 (gold 332 контекста, 640 сущностей в скоупе):

| stack | micro P | micro R | micro F1 | FIO R | DOB R | MEDCARD R | AGE R | латентность |
|---|---|---|---|---|---|---|---|---|
| regex | 1.000 | 0.344 | 0.512 | 0.000 | 1.000 | 1.000 | 0.000 | 0.04 с / 332 |
| rubert | 0.409 | 0.753 | 0.530 | 1.000 | 0.000 | 0.000 | 0.000 | 14 с / 332 (GPU) |
| gliner_multi_pii-v1 | 0.951 | 0.826 | 0.884 | 0.975 | 1.000 | 0.250 | 0.000 | 2.47 с / контекст |
| llm (home-stable, батч 6) | см. `REPORT-pii.md` | | | | | | | |
| **hybrid (прод)** | **1.000** | **0.987** | **0.993** | 0.975 | 1.000 | 1.000 | 1.000 | 0.042 с / 332 (GPU) |

Разбор промахов hybrid: 8/320 ФИО — записи целиком строчными буквами
(«сидорова галина»), rubert их не размечает; сознательно не лечим
эвристикой (ложные срабатывания на клиническом тексте). Прочие категории —
1.000, `DOC`-категории различаются по `Finding.category`.

GLiNER: сильный zero-shot по ФИО/контактам, но не знает форматов СНИЛС/ИНН/
ОМС/медкарты, путает ИБ с ИНН и не умеет возраст-комбо; 2.5 с на контекст —
дорого для синхронного egress.

## Публичный pii_benchmark

```bash
bench/.venv/bin/python bench/ner/fetch_benchmark.py          # → gitignored data/
bench/.venv/bin/python bench/ner/run_ner_bench.py --candidates regex,natasha,rubert,combo
```

Замер 2026-10-24 (2841 строка, exact / overlap F1):

| backend | FIO exact/overlap | ВСЕГО exact | ВСЕГО overlap |
|---|---|---|---|
| regex (ужесточённые правила) | 0.000 / 0.000 | 0.127 | 0.127 |
| natasha | 0.168 / 0.456 | 0.115 | 0.210 |
| rubert | **0.933 / 0.947** | 0.468 | 0.605 |
| combo (regex ∪ rubert) | 0.933 / 0.947 | **0.489** | **0.631** |

Отличие от прошлого замера (0.493 / 0.637) — в пределах шума: добавленные
контрольные числа/маркеры повысили точность на клинических текстах и слегка
снизили полноту на шумных новостных; FIO стабилен (0.933/0.947). Публичный
F1 размыт 22 категориями, часть которых (деньги, URL, события) к egress-пути
не относится, — поэтому основная метрика — клинический корпус.

## Предсказания

`clinical_contexts/preds/` — JSONL предсказаний и `*.report.json` (gitignored:
модельные артефакты пересоздаются командами выше). Корпус
`clinical_contexts/corpus.jsonl` коммитится: он синтетический, gold вычислен
по конструкции (без ручной разметки и без участия моделей).
