"""Пересборка бенч-снапшота КБ с другим размером чанка.

Чанки строятся тем же кодом, что и прод (`app.enrichment.index.build_documents`
для гайдлайнов ВОЗ и статей, `app.kb_seed.build_corpus` для сида), поэтому
метрики бенча сравнимы с продовым путём при том же `CC_KB_CHUNK_CHARS`.

Пример:
    .venv/bin/python bench/rechunk_corpus.py --chars 600 --overlap 120 \
        --out bench/data/kb_corpus_600.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "api", ROOT / "core"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.enrichment.index import build_documents  # noqa: E402
from app.models import KBWork  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chars", type=int, default=settings.kb_chunk_chars)
    ap.add_argument("--overlap", type=int, default=settings.kb_chunk_overlap)
    ap.add_argument("--db", default=settings.database_url)
    ap.add_argument("--out", default=str(ROOT / "bench" / "data" / "kb_corpus_chunked.jsonl"))
    ap.add_argument(
        "--carry",
        default=str(ROOT / "bench" / "data" / "kb_corpus.jsonl"),
        help="снапшот, из которого переносятся НЕ-гайдлайновые чанки сида",
    )
    args = ap.parse_args()

    engine = create_engine(args.db, future=True)
    rows: list[dict] = []
    with Session(engine) as session:
        works = session.scalars(
            select(KBWork).where(KBWork.status.in_(("approved", "auto-published")))
        ).all()
        for work in works:
            for doc in build_documents(work, chunk_chars=args.chars, chunk_overlap=args.overlap):
                rows.append(
                    {
                        "id": f"{doc['guideline_id']}#{doc['chunk_index']}",
                        "guideline_id": doc["guideline_id"],
                        "source_title": doc["source_title"],
                        "topic": doc["topic"],
                        "text": doc["text"],
                    }
                )

    # Чанки сида (шкалы, препараты, триаж, статьи, метаданные КР) чанкер не
    # режет — переносим их из прежнего снапшота как есть.
    carried = 0
    carry_path = Path(args.carry)
    if carry_path.exists():
        for line in carry_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if str(row.get("guideline_id", "")).startswith("who-"):
                continue
            rows.append(row)
            carried += 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    lengths = sorted(len(r["text"]) for r in rows)
    by_topic: dict[str, int] = {}
    for r in rows:
        by_topic[r["topic"]] = by_topic.get(r["topic"], 0) + 1
    print(f"чанков: {len(rows)} (chars={args.chars}, overlap={args.overlap})")
    print(f"длины: min {lengths[0]} / медиана {lengths[len(lengths) // 2]} / max {lengths[-1]}")
    print("по разделам:", by_topic)
    print(f"перенесено чанков сида: {carried}")
    print(f"записано: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
