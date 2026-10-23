"""Кеш корпуса KB (kb_seed.build_corpus): контракт TTL, инвалидация, неизменяемость.

build_corpus() зовётся на запрос агента (лексический fallback в search_kb) и до
кеша каждый раз ходил в БД за препаратами. Тесты держат поведение: снимок
неизменяем, промах кеша — единственный поход в БД, ошибки не кешируются,
TTL/clear_corpus_cache() дают свежие данные.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import event, update

from app import db, kb_seed
from app.config import settings
from app.kb_seed import build_corpus, clear_corpus_cache


@pytest.fixture()
def queries(engine):
    """Счётчик SQL-запросов через движок (before_cursor_execute)."""
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", _record)
    yield seen
    event.remove(db.engine, "before_cursor_execute", _record)


def test_second_call_is_cache_hit_without_db_query(queries):
    clear_corpus_cache()
    build_corpus()  # прогрев
    assert queries, "холодный build_corpus обязан сходить в БД за препаратами"

    queries.clear()
    build_corpus()
    build_corpus()
    assert queries == [], f"кеш-хит не должен обращаться к БД: {queries}"


def test_cache_hit_is_at_least_10x_faster(queries):
    clear_corpus_cache()
    build_corpus()  # прогрев пула соединений и дисковых кешей

    cold = []
    for _ in range(5):
        clear_corpus_cache()
        t0 = time.perf_counter()
        build_corpus()
        cold.append(time.perf_counter() - t0)

    warm = []
    build_corpus()
    for _ in range(200):
        t0 = time.perf_counter()
        build_corpus()
        warm.append(time.perf_counter() - t0)

    cold_p50 = sorted(cold)[len(cold) // 2]
    warm_p50 = sorted(warm)[len(warm) // 2]
    assert warm_p50 * 10 < cold_p50, (
        f"кеш-хит недостаточно быстрый: cold p50 {cold_p50 * 1e3:.3f} ms, "
        f"warm p50 {warm_p50 * 1e3:.3f} ms"
    )


def test_mutation_is_invisible_until_invalidated():
    """Запись в drugs не видна кешу до clear_corpus_cache() (TTL ещё не истёк)."""
    from app.models import Drug

    clear_corpus_cache()
    before = build_corpus()
    assert any("АВИАНДР" in d["text"] for d in before)

    with db.SessionLocal() as s:
        s.execute(update(Drug).where(Drug.drug_id == "aviandr").values(trade_name="АВИАНДР-ТЕСТ"))
        s.commit()

    stale = build_corpus()
    assert stale is before, "до истечения TTL и без инвалидации отдаётся тот же снимок"
    assert not any("АВИАНДР-ТЕСТ" in d["text"] for d in stale), "до инвалидации кеш отдаёт старый снимок"

    clear_corpus_cache()
    fresh = build_corpus()
    assert any("АВИАНДР-ТЕСТ" in d["text"] for d in fresh)
    assert fresh is not before


def test_ttl_expiry_rebuilds_corpus(monkeypatch):
    from app.models import Drug

    clock = [1000.0]
    monkeypatch.setattr(kb_seed.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "kb_corpus_ttl_seconds", 60.0)

    clear_corpus_cache()
    build_corpus()
    with db.SessionLocal() as s:
        s.execute(update(Drug).where(Drug.drug_id == "aviandr").values(trade_name="АВИАНДР-TTL"))
        s.commit()

    clock[0] += 59.0
    assert not any("АВИАНДР-TTL" in d["text"] for d in build_corpus()), "TTL ещё не истёк"

    clock[0] += 2.0
    assert any("АВИАНДР-TTL" in d["text"] for d in build_corpus()), "после TTL корпус пересобран"


def test_ttl_zero_disables_cache(queries, monkeypatch):
    monkeypatch.setattr(settings, "kb_corpus_ttl_seconds", 0.0)
    clear_corpus_cache()
    build_corpus()
    queries.clear()
    build_corpus()
    assert len(queries) == 1, "при TTL=0 каждый вызов собирает корпус заново"


def test_errors_are_not_cached(monkeypatch, queries):
    """Недоступная БД не должна «залипать» в кеше: следующий вызов пробует снова."""

    def _boom() -> list[dict]:
        raise RuntimeError("db down")

    clear_corpus_cache()
    monkeypatch.setattr(kb_seed, "_drug_docs", _boom)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            build_corpus()
    assert kb_seed._corpus_cache == {}

    monkeypatch.undo()
    queries.clear()
    docs = build_corpus()
    assert docs and queries, "после восстановления БД корпус собирается заново"


def test_snapshot_is_immutable():
    clear_corpus_cache()
    docs = build_corpus()
    with pytest.raises(TypeError):
        docs[0]["text"] = "подмена"  # type: ignore[index]
