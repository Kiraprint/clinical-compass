# Бенчмарк RU PII / NER для деперсонализации (docs/12)

Честная оценка кандидатов на **публичном** датасете
[redmadrobot-rnd/pii_benchmark](https://huggingface.co/datasets/redmadrobot-rnd/pii_benchmark)
(MIT, 2841 строка, BIO-разметка, 22 категории реальных русских ПДн).
Текст датасета не коммитится — `fetch_benchmark.py` кладёт его в gitignored
`bench/ner/data/` (та же политика, что и `bench/` для retrieval).

## Кандидаты

| backend | что это | лицензия | FIO F1 (exact) | FIO F1 (overlap) | ВСЕГО exact | ВСЕГО overlap |
|---|---|---|---|---|---|---|
| `regex` | `core/compass/depersonalize.py`, структурные сущности (без deny-list) | — | 0.000 | 0.000 | 0.094 | 0.134 |
| `natasha` | `NewsNERTagger` (slovnet news v1), PER→ФИО, LOC→адрес | MIT | 0.168 | 0.456 | 0.115 | 0.210 |
| `rubert` | [redmadrobot-rnd/rubert-base-pii-ner](https://huggingface.co/redmadrobot-rnd/rubert-base-pii-ner), ruBert-base, 14 категорий ПДн | Apache-2.0 | **0.933** | **0.947** | 0.468 | 0.605 |
| `combo` | regex ∪ rubert (полный гвард) | — | 0.933 | 0.947 | **0.493** | **0.637** |

Вывод: **rubert в 5.5x точнее natasha по ФИО** (0.933 vs 0.168 exact; 0.947 vs
0.456 overlap — совпадает с заявленным в карточке модели 0.947 для PERSON+LOCATION).
Полный гвард (regex для телефонов/email/документов + rubert для ФИО/адресов) даёт
лучший суммарный F1. Поэтому в проде доступен backend `CC_DEPERSONALIZE_NER=rubert`
(extra `api[ner-rubert]`), natasha остаётся лёгкой альтернативой, demo-дефолт — `off`.

Колонки `exact` (точное совпадение спана) и `overlap` (середина предсказанного
спана внутри золотого) — вторая метрика ближе к практике маскирования.

## Запуск

```bash
bench/.venv/bin/pip install natasha "transformers>=5" "huggingface-hub>=1.31,<2.0"  # + torch cu128 для GPU
bench/.venv/bin/python bench/ner/fetch_benchmark.py
bench/.venv/bin/python bench/ner/run_ner_bench.py --candidates regex,natasha,combo
# rubert: скачать модель локально (711 МБ) и указать путь
NER_MODEL=/path/to/rubert-pii-model bench/.venv/bin/python bench/ner/run_ner_bench.py --candidates rubert,combo
```

Оговорки: датасет — общие ПДн (не клинические), 1 модель rubert; метрики на CPU/GPU
зависят от железа. Для клинического домена NER-качество стоит перепроверить на
разметке историй болезни (аналогично `bench/history/`).
