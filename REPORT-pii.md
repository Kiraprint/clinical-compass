# REPORT: PII-деперсонализация egress'а (recall/precision, fail-closed)

Ветка `feat/pii-98`, worktree `.worktrees/pii-ner`, база `fac71ad`.

## Что сделано

1. **Клинический gold-корпус** `bench/ner/clinical_contexts/` — 332
   синтетических контекста четырёх реальных форм egress'а (чат пациента 124,
   дневник 72, вопрос врача 76, батч FR-31 60). Gold-спаны вычислены по
   конструкции (шаблон = литералы + подстановки из пулов), без ручной разметки
   и без участия моделей. Коммитится `corpus.jsonl` + `build_corpus.py`.
   Категории: FIO, PHONE, EMAIL, ADDRESS, DOB, SNILS, INN, OMS, PASSPORT,
   MEDCARD, AGE (возраст только в связке с ФИО).
2. **Матрица кандидатов** `bench/ner/run_clinical_bench.py` (regex, rubert,
   GLiNER, LLM через шлюз, hybrid) + сохранение предсказаний и отчётов.
   Публичный бенчмарк оставлен как вторичная проверка (`run_ner_bench.py`).
3. **Прод-путь `hybrid`** (`CC_DEPERSONALIZE_NER=hybrid`): span-based
   маскирование (`core/compass/depersonalize.py`), rubert только для ФИО/адреса,
   структурные правила с контрольными числами (СНИЛС/ИНН/ОМС), нормализация
   телефонов/дат, все формы `8 926 123-45-67` / `+79261234567` / `9261234567`,
   ОМС с пробелами и слитно, паспорт, медкарта/ИБ, дата рождения в 4 форматах,
   возраст-комбо с ФИО.
4. **Fail-closed:** `_backend_unavailable()` → в `CC_PROD_MODE` `503
   DEPERSONALIZATION_UNAVAILABLE` (сырой текст не уходит), вне prod — regex-
   деградация с логом. `Settings()` в prod с LLM-egress требует
   `CC_DEPERSONALIZE_NER != off`.
5. **FR-31:** в LLM уходят только реплики `PATIENT` (хронология сохранена),
   таймаут `CC_CHAT_INSIGHTS_TIMEOUT=60` + один ретрай, лог без текста.
6. Тесты: `core/tests/test_depersonalize.py` (21), `api/tests/test_depersonalize.py`
   (новый, 29 collected), `api/tests/test_depersonalize_ner.py` (24).

## Измерения (клинический корпус, 332 контекста)

Метрика: entity-level, совпадение = пересечение ≥50% длины более короткого
спана. `P`/`R` — micro (все сущности в скоупе).

| stack | micro P | micro R | micro F1 | FIO R | ADDRESS R | DOB R | DOC R (SNILS/INN/OMS/PASS/MED) | AGE R |
|---|---|---|---|---|---|---|---|---|
| regex (ядро, без NER) | 1.000 | 0.344 | 0.512 | 0.000 | 1.000 | 1.000 | 1.000/1.000/1.000/1.000/1.000 | 0.000 |
| rubert (модель без правил) | 0.409 | 0.753 | 0.530 | 1.000 | 1.000 | 0.000 | 0.850/1.000/0.958/1.000/0.000 | 0.000 |
| gliner_multi_pii-v1 | 0.951 | 0.826 | 0.884 | 0.975 | 0.875 | 1.000 | 1.000/1.000/1.000/1.000/0.250 | 0.000 |
| llm (home-stable, батч 6) | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000/1.000/1.000/1.000/1.000 | 1.000 |
| **hybrid (прод)** | **1.000** | **0.987** | **0.993** | 0.975 | 1.000 | 1.000 | 1.000/1.000/1.000/1.000/1.000 | 1.000 |

Hybrid: macro P = 1.000, macro R = 0.998. Все промахи — 8/320 ФИО, записанных
целиком строчными («сидорова галина»); rubert такие не размечает. Ложных
срабатываний hybrid — 0.

### Разбор промахов (hybrid)

| id | категория | значение | почему |
|---|---|---|---|
| cc-008 | FIO | `сидорова галина` | оба слова строчными, NER не видит |
| cc-014/016 и др. (всего 8) | FIO | `иванов пётр`, `петрова анна` | то же |

### Публичный pii_benchmark (сопоставимость)

2841 строка, exact/overlap F1: rubert FIO 0.933/0.947 (стабилен), combo
**0.489/0.631** (было 0.493/0.637 — шум после ужесточения валидаторов).
Основной метрикой публичный F1 не является: 22 категории, часть вне скоупа.

## Замер зависимостей/латентности

- rubert-base-pii-ner на RTX 5090: 332 контекста за 13.9 с после прогрева
  (~42 мс/контекст), модель грузится один раз на процесс.
- GLiNER: 820 с / 332 = 2.47 с на контекст (zero-shot, дорого для синхронного
  egress).
- LLM (`home-stable` через omniroute, батч 6 контекстов): **P/R = 1.000/1.000**
  на корпусе; 56 вызовов, 2063 с суммарно = **6.2 с/контекст**, 63 078 prompt
  + 70 130 completion токенов (~400 ток./контекст). Шлюз self-hosted, денежная
  стоимость = 0; токены — прокси стоимости. Точность 1.000 ожидаема: LLM видит
  явный список категорий в промпте, но не gold — годится как офлайн-аудит,
  не для синхронного egress (docs/12).

## FR-31: шум vs только пациент

Один и тот же диалог: с репликами помощника («Спасибо, что поделились…») и
врача — **0 фактов**; только сообщения пациента — **4 факта** (medication,
sleep, adherence, substance). Поэтому `_dialogue_text` фильтрует
`sender == "PATIENT"`, сохраняя хронологический порядок; ответы помощника и
врача в LLM не уходят.

## Как перемерить

```bash
cd /home/kir/Code/clinical-compass/.worktrees/pii-ner

# 1) gold-корпус (детерминированно; коммитится)
bench/.venv/bin/python bench/ner/clinical_contexts/build_corpus.py

# 2) in-process кандидаты (GPU)
bench/.venv/bin/python bench/ner/run_clinical_bench.py --predict regex,rubert,hybrid --save

# 3) GLiNER в своём окружении (gliner + transformers<5.17)
/tmp/pii-gliner/bin/python bench/ner/run_clinical_bench.py --predict gliner --save

# 4) LLM-кандидат (ключ из infra/.env)
CC_ASSISTANT_LLM_URL=http://172.17.0.1:20133/v1 CC_ASSISTANT_LLM_API_KEY=... \
  bench/.venv/bin/python bench/ner/run_clinical_bench.py --predict llm --save --llm-batch 6

# 5) отчёт по сохранённым предсказаниям
bench/.venv/bin/python bench/ner/run_clinical_bench.py --eval-only

# 6) публичный бенчмарк (вторичная проверка)
bench/.venv/bin/python bench/ner/fetch_benchmark.py
bench/.venv/bin/python bench/ner/run_ner_bench.py --candidates regex,natasha,rubert,combo

# 7) тесты (своя БД/коллекция)
cd api
CC_ALLOW_DEMO_SECRETS=true CC_TEST_DATABASE_URL=postgresql+psycopg://compass:compass@127.0.0.1:5432/cc_pii_ner \
PYTHONPATH=$PWD:$PWD/../core /home/kir/Code/clinical-compass/.venv/bin/python -m pytest \
  tests/test_depersonalize.py tests/test_depersonalize_ner.py -q
cd .. && PYTHONPATH=core /home/kir/Code/clinical-compass/.venv/bin/python -m pytest core/tests/test_depersonalize.py -q
```

## Принятые ограничения

- 2.5% ФИО целиком строчными не детектируются rubert; эвристика «два
  строчных слова» отключена осознанно (ложные срабатывания на клиническом
  тексте). Recall 0.987 ≥ 0.98.
- Медкарта без маркера (`№ 456123` без «медкарта/ИБ/история болезни») не
  маскируется: номер неотличим от произвольного числа.
- Дата рождения без маркера не маскируется (клиническая дата); это политика
  docs/12, закреплена тестом.
- GLiNER требует отдельного окружения (transformers<5.17); пакет устанавливался
  в `/tmp/pii-gliner` через `--system-site-packages`, общий `bench/.venv` не
  изменён (transformers восстановлен на 5.20.1).
- `hybrid` в тестах использует подменённый extractor (модель не грузится);
  реальный rubert проверен бенчмарком на GPU.
