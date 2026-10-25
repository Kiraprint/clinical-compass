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

## Phase 2: GPU-sidecar (`CC_DEPERSONALIZE_NER=remote`)

Прод-форма: один сервис `ml` (nvidia runtime) отдаёт эмбеддинги, реранк и
маскирование; api-образ остаётся без torch.

- backend `remote` → `ml_client.mask(text, names)` (общий клиент
  `api/app/services/ml_client.py`, скопирован 1:1 из `feat/ml-client`,
  sha256 `61dfdc79…`), ответ `{text, spans, model}` маппится в `Span`'ы ядра:
  категории FIO/ADDRESS/PHONE/EMAIL/DOB/AGE → одноимённые виды,
  SNILS/INN/OMS/PASSPORT/MEDCARD → `DOC` + точная `Finding.category`;
  дальше — тот же `resolve_spans`/`mask_spans`, что у hybrid.
- Конфиг: `CC_ML_URL`, `CC_ML_TOKEN` (`X-CC-ML-Token`), `CC_ML_TIMEOUT=5` —
  в одном экземпляре с веткой клиента (при мерже конфликт полей ожидаем).
- Fail-closed без изменений: sidecar недоступен/5xx/битый JSON → в
  `CC_PROD_MODE` `503 DEPERSONALIZATION_UNAVAILABLE` до вызова LLM; вне prod —
  regex-деградация с WARNING.
- Стартовая валидация: в prod с LLM-egress принимается `remote` + непустой
  `CC_ML_URL` **или** локальный `hybrid/rubert/natasha`; `off` и `remote` без
  URL — `ValueError` с перечислением обоих вариантов.
- Скруб в `assistant_chat` уведён в `anyio.to_thread.run_sync` (HTTP-вызов не
  блокирует event loop); `chat_insights` уже так делал.
- Тесты: `api/tests/test_depersonalize_remote.py` (11) — фейковый HTTP-сервер
  `/v1/mask` без GPU: структурные форматы, ФИО с инициалами, 8 остаточных
  промахов строчных ФИО, проброс `names`/токена, fail-closed с проверкой
  «ноль вызовов LLM», деградация вне prod, все 332 строки клинического корпуса,
  приёмка prod-конфига. Итог прогона: `test_depersonalize*.py` +
  `test_assistant_threads.py` — 85 passed.
- Латентность: 42 мс инференса на sidecar'е (RTX 5090) + ~1 мс HTTP на
  loopback; таймаут 5 с с одним повтором на сетевой сбой/5xx.

Как перемерить remote:

```bash
cd api
CC_ALLOW_DEMO_SECRETS=true CC_TEST_DATABASE_URL=postgresql+psycopg://compass:compass@127.0.0.1:5432/cc_pii_ner \
PYTHONPATH=$PWD:$PWD/../core /home/kir/Code/clinical-compass/.venv/bin/python -m pytest \
  tests/test_depersonalize_remote.py -q
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
- `remote` в прод-контуре полностью зависит от sidecar'а: при его недоступности
  egress честно отказывает (503), а не отдаёт сырой текст; латентность и
  алерты — на `/health` и таймаутах клиента. Модель внутри sidecar'а — тот же
  rubert, поэтому остаточный промах строчных ФИО сохраняется (docs/10 R25).

---

## Обновление 2026-10-25: форматы представления, строчные ФИО, медкарта без маркера

Ветка `feat/pii-cases`, worktree `.worktrees/pii-cases`, база main `3a84d0f`.
Раздел выше — исторический замер на 332 контекстах; этот раздел его заменяет.

Закрыто два класса промахов (было 8/320 FIO, recall 0.987):

- **ФИО целиком строчными** (`сидорова галина`, `иванов пётр сергеевич`):
  структурная эвристика ядра — морфология фамилии (‑ов/‑ев/‑ёв/‑ин/‑ын/‑ский/
  ‑цкий/‑ко/‑енко/‑ук/‑ян/‑дзе/‑швили…) + имя из списка частых имён.
  Дополнительно: инициалы после/до фамилии (`Иванов П.С.`, `П.С. Иванов`) и
  одиночная капс/титульная фамилия из списка частых (`ПЕТРОВА`). Словарно-
  агностично: список имён не привязан к ростеру; клинические слова не проходят
  якорь-имя (`больница`, `температура`, `голова`, `аспирин`).
- **Медкарта без слова-маркера**: `№ 123456` (5–8 цифр); формы `карта 123456`,
  `ИБ 456789` работали и раньше.

Заодно расширены форматы: телефон с префиксом `7` без «+» (`7 926 123-45-67`,
`7(916)555-77-33`, `79261234567`); СНИЛС/ИНН группами через пробелы/дефисы;
паспорт с серией по 2 цифры (`45 09 123456`); адрес «живу в Москве на
Ленина 5»; возраст «мне 34» (маскируется только в связке с ФИО).

### Измерения

Корпус: 418 контекстов (332 базовых + 86 кейсов форматов: 66 позитивных,
20 negative), 700 gold-сущностей. Команда:

```bash
bench/.venv/bin/python bench/ner/run_clinical_bench.py --predict regex,hybrid
```

| stack | micro P | micro R | micro F1 | FP |
|---|---|---|---|---|
| regex (правила ядра) | **1.000** | **1.000** | **1.000** | 0 |
| rubert (модель без правил) | 0.426 | 0.747 | 0.542 | 706 |
| **hybrid (прод)** | **1.000** | **1.000** | **1.000** | 0 |

Per-category (regex = hybrid на этом корпусе; P/R/F1 = 1.000 у всех):

| категория | gold | tp | fp | fn |
|---|---|---|---|---|
| FIO | 338 | 338 | 0 | 0 |
| PHONE | 48 | 48 | 0 | 0 |
| EMAIL | 17 | 17 | 0 | 0 |
| ADDRESS | 31 | 31 | 0 | 0 |
| DOB | 57 | 57 | 0 | 0 |
| SNILS | 26 | 26 | 0 | 0 |
| INN | 20 | 20 | 0 | 0 |
| OMS | 28 | 28 | 0 | 0 |
| PASSPORT | 20 | 20 | 0 | 0 |
| MEDCARD | 24 | 24 | 0 | 0 |
| AGE | 91 | 91 | 0 | 0 |

Negative-кейсы (тревога, бессонница, «5 дней», «2 раза в день», клинические
даты без маркера, «мне 34» без ФИО, ГКБ № 2, «на 0,5 ставки») — 0 ложных
срабатываний; дополнительно прогнана ручная батарея из 30 клинических фраз —
0 изменений текста. Правки по ревью: NER-адреса без опорного слова
(`кабинет 5`, `2`) отсеиваются `is_plausible_address_span`; пересекающиеся
спаны одного вида объединяются, а не обрезаются (иначе NER-фрагмент
«Ленина 5» разрезал структурный адрес и давал FP).

Остаточные пропуски вне gold: нерусские имена; фамилия без фамильного
окончания и без имени рядом; одиночная фамилия строчными; падежные формы
имени вне deny-list («Иванову Петру» — эвристика ждёт именительный падеж);
инициал без второго (`Иванов П.`); номер медкарты короче 5 цифр; возраст
без ФИО.

Тесты: `core/tests` 170 passed; `api/tests/test_depersonalize*.py` 76 passed;
`api/tests/test_assistant.py` + `test_chat_insights.py` 33 passed.
