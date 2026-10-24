"""Чанкер КБ: размер/перекрытие и общий контракт сида и обогащения (docs/08 §3).

Тесты без БД и Qdrant: chunk_text — чистая функция, сид проверяется на
временном файле вместо data/guidelines_cache.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.enrichment.config import EnrichmentConfig
from app.enrichment.index import chunk_text
from app.services.reranker import best_window


def test_chunk_text_short_text_single_chunk() -> None:
    assert chunk_text("короткий текст", 600, 120) == ["короткий текст"]
    assert chunk_text("", 600, 120) == []


def test_chunk_text_respects_size_and_keeps_paragraphs() -> None:
    text = "\n\n".join(f"Абзац {i}. " + "слово " * 40 for i in range(6))
    chunks = chunk_text(text, 600, 120)
    assert len(chunks) > 1
    assert all(len(c) <= 600 for c in chunks)
    # абзацы не разорваны: каждый кусок начинается с «Абзац N»
    assert all(c.lstrip().startswith("Абзац") for c in chunks)


def test_chunk_text_long_paragraph_overlaps() -> None:
    paragraph = "".join(f"{i:05d} " for i in range(400))  # один абзац, ~2400 знаков
    chunks = chunk_text(paragraph, 600, 120)
    assert len(chunks) >= 4
    assert all(len(c) <= 600 for c in chunks)
    # перекрытие: хвост куска (120 знаков) присутствует в следующем (шаг 480)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev[-120:].strip() in nxt


def test_chunk_text_no_duplicates_and_terminates() -> None:
    text = ("абзац " * 50 + "\n\n") * 20
    chunks = chunk_text(text, 300, 100)
    assert chunks == list(dict.fromkeys(chunks))
    assert all(len(c) <= 300 for c in chunks)


def test_enrichment_defaults_follow_kb_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CC_ENRICH_CHUNK_CHARS", raising=False)
    monkeypatch.delenv("CC_ENRICH_CHUNK_OVERLAP", raising=False)
    cfg = EnrichmentConfig.from_env()
    assert cfg.chunk_chars == settings.kb_chunk_chars
    assert cfg.chunk_overlap == settings.kb_chunk_overlap


def test_seed_guidelines_use_configured_chunk_size(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import kb_seed

    data = {
        "guidelines": [
            {
                "id": "t1",
                "code": "КР1",
                "title": "Тест",
                "revision": "2024",
                "developer": "МЗ",
                "summary": "кратко",
                "rubricator_url": "https://example.org",
            }
        ]
    }
    gfile = tmp_path / "guidelines.json"
    gfile.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "t1.txt").write_text("\n\n".join(f"Абзац {i}. " + "слово " * 60 for i in range(8)), encoding="utf-8")

    monkeypatch.setattr(kb_seed, "_GUIDELINES_FILE", gfile)
    monkeypatch.setattr(kb_seed, "_GUIDELINES_CACHE", cache)
    monkeypatch.setattr(settings, "kb_chunk_chars", 600)
    monkeypatch.setattr(settings, "kb_chunk_overlap", 120)

    docs = kb_seed._guideline_docs(with_full_text=True)
    body = [d for d in docs if d["guideline_id"].startswith("kr-t1-t")]
    assert len(body) > 1
    assert all(len(d["text"]) <= 600 for d in body)
    assert [d["guideline_id"] for d in body] == [f"kr-t1-t{i}" for i in range(len(body))]
    # метаданный чанк самой КР остаётся отдельным и первым
    assert docs[0]["guideline_id"] == "kr-t1"


def test_best_window_finds_answer_beyond_head() -> None:
    text = "шапка " * 120 + " Ответ: сертралин 50 мг при депрессии. " + "хвост " * 120
    window = best_window(text, "какая доза сертралина при депрессии", 600)
    assert len(window) <= 600
    assert "сертралин" in window
    # короткий чанк возвращается целиком, без изменений
    assert best_window("коротко", "запрос", 600) == "коротко"
    assert best_window(text, "запрос", 0) == text
