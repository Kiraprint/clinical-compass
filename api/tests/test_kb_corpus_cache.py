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
    """Счётчик SQL-запросов через sync-движок (before_cursor_execute).

    async-db: request-path не зовёт build_corpus() без drug_docs, поэтому
    препараты в кеш читает SyncSessionLocal — считаем именно sync-движок.
    """
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(db.sync_engine, "before_cursor_execute", _record)
    yield seen
    event.remove(db.sync_engine, "before_cursor_execute", _record)


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

    with db.SyncSessionLocal() as s:
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
    with db.SyncSessionLocal() as s:
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


def test_request_path_drug_docs_are_not_cached(queries):
    """build_corpus(drug_docs=...) отдаёт переданные препараты и не ходит в БД.

    Request-path передаёт срез из async-сессии запроса: такой список нельзя ни
    кешировать, ни подменять прочитанным из БД.
    """
    clear_corpus_cache()
    per_request = [
        {"guideline_id": "drug-req-1", "source_title": "Req", "topic": "ЛЕКАРСТВА", "text": "ЗАПРОСНЫЙ ПРЕПАРАТ"}
    ]
    docs = build_corpus(drug_docs=per_request)
    assert any(d["guideline_id"] == "drug-req-1" for d in docs)
    assert any("ЗАПРОСНЫЙ ПРЕПАРАТ" in d["text"] for d in docs)
    assert queries == [], f"request-path не должен читать препараты из БД: {queries}"

    # В кеше request-path препаратов нет: следующий вызов с drug_docs=None
    # собирает препараты из БД.
    default_docs = build_corpus()
    assert not any(d["guideline_id"] == "drug-req-1" for d in default_docs)

    per_request.append(
        {"guideline_id": "drug-req-2", "source_title": "Req", "topic": "ЛЕКАРСТВА", "text": "ВТОРОЙ"}
    )
    assert not any(d["guideline_id"] == "drug-req-2" for d in docs), "переданный список не живёт в кеше"


def test_hash_rerank_without_drugs_never_opens_sync_session(queries):
    """Лексический fallback агента не ходит в БД: `corpus_drugs is None` = «нет препаратов».

    `_hash_rerank` исполняется в рабочем потоке offload'а агента, где sync-сессия
    запрещена (docs/03 §12): препараты грузит вызывающий через async-сессию
    запроса (`load_drug_docs`), а None не должен превращаться в поход в БД.
    """
    from app.services.agent import _hash_rerank

    clear_corpus_cache()
    build_corpus(drug_docs=[])  # прогрев статики без препаратов
    queries.clear()

    hits = _hash_rerank("порог PHQ-9", [], 5, topics=(), exclude_ids=None, corpus_drugs=None)

    assert hits, "статическая часть корпуса должна находиться лексически"
    assert not any(h["guideline_id"].startswith("drug-") for h in hits)
    assert queries == [], f"sync-сессия в потоке агента запрещена: {queries}"
