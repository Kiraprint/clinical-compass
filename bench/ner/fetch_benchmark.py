"""Скачивает Russian PII NER Benchmark (MIT, redmadrobot-rnd/pii_benchmark).

Текст датасета не коммитится (политика репозитория: сторонний текст — только
в gitignored bench/ner/data/). Скрипт идемпотентен: повторный запуск
переиспользует файл, если он уже есть.

    python bench/ner/fetch_benchmark.py
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

URL = "https://huggingface.co/datasets/redmadrobot-rnd/pii_benchmark/resolve/main/test.csv"
OUT = Path(__file__).parent / "data" / "pii_benchmark_test.csv"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists() and OUT.stat().st_size > 10_000:
        print(f"уже скачано: {OUT} ({OUT.stat().st_size} байт)")
        return 0
    print(f"скачиваю {URL}")
    with urllib.request.urlopen(URL, timeout=120) as resp:
        data = resp.read()
    if not data.startswith(b"text,tokens,ner_tags"):
        print("ОШИБКА: неожиданный формат ответа (не CSV)", file=sys.stderr)
        return 1
    OUT.write_bytes(data)
    print(f"готово: {OUT} ({len(data)} байт)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
