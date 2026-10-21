"""Пайплайн обогащения: реестр, гейты, индекс, воспроизводимость (реальные PG+Qdrant)."""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("qdrant_client")

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm
from sqlalchemy import select

from app.config import settings
from app.enrichment import pipeline, review
from app.enrichment.config import EnrichmentConfig
from app.enrichment.index import ARTICLE_TOPIC
from app.models import KBRun, KBWork
from app.services import kb as kb_store
from app.services.embeddings import HashEmbedder

from .enrichment_fixtures import make_doc, make_guideline


def _qdrant_available() -> bool:
    try:
        QdrantClient(url=settings.qdrant_url, timeout=2).get_collections()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _qdrant_available(), reason="Qdrant недоступен (CC_QDRANT_URL)")


@pytest.fixture()
def qdrant(monkeypatch):
    """Изолированная коллекция на тест: не пересекаемся с /assistant-тестами."""
    name = f"kb_enrich_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(kb_store, "COLLECTION", name)
    client = QdrantClient(url=settings.qdrant_url)
    yield client
    if client.collection_exists(name):
        client.delete_collection(name)


@pytest.fixture()
def cfg() -> EnrichmentConfig:
    return EnrichmentConfig(offline=True, chunk_chars=1200, chunk_overlap=100)


def _points(client, work: KBWork) -> int:
    if not client.collection_exists(kb_store.COLLECTION):
        return 0
    result = client.count(
        collection_name=kb_store.COLLECTION,
        count_filter=qm.Filter(
            must=[qm.FieldCondition(key="work_id", match=qm.MatchValue(value=str(work.id)))]
        ),
        exact=True,
    )
    return result.count


def test_guideline_with_confirmed_license_is_auto_published(db_session, qdrant, cfg):
    doc = make_guideline()
    summary = pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    assert summary.status == "ok"
    assert summary.stats["published"] == 1

    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    assert work is not None
    assert work.status == "approved"
    assert work.indexed_hash == work.content_hash
    assert work.chunk_count > 0
    assert _points(qdrant, work) > 0

    payload = qdrant.scroll(
        collection_name=kb_store.COLLECTION,
        scroll_filter=qm.Filter(
            must=[qm.FieldCondition(key="work_id", match=qm.MatchValue(value=str(work.id)))]
        ),
        limit=1,
        with_payload=True,
    )[0][0].payload
    assert payload["topic"] == "РЕКОМЕНДАЦИИ"
    assert payload["source_url"] == doc.url
    assert "CC BY-NC-SA 3.0 IGO" in payload["license"]


def test_guideline_without_license_waits_for_human(db_session, qdrant, cfg):
    doc = make_guideline(
        license="",
        text=make_guideline().text.replace("Creative Commons", "Proprietary"),
    )
    summary = pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    assert work is not None
    assert work.status == "candidate"
    assert work.indexed_hash is None
    assert _points(qdrant, work) == 0
    assert summary.stats["published"] == 0


def test_article_is_candidate_until_approved(db_session, qdrant, cfg):
    doc = make_doc()
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    assert work is not None and work.status == "candidate"
    assert _points(qdrant, work) == 0

    review.approve(db_session, qdrant, work.id, None, cfg)
    assert work.status == "approved"
    assert _points(qdrant, work) == work.chunk_count > 0

    hits = kb_store.hybrid_search(
        qdrant,
        HashEmbedder(),
        "cognitive behavioural therapy generalised anxiety",
        limit=5,
    )
    assert any(hit["guideline_id"] == doc.guideline_id for hit in hits)

    hits_excluded = kb_store.hybrid_search(
        qdrant,
        HashEmbedder(),
        "cognitive behavioural therapy generalised anxiety",
        limit=5,
        exclude_ids={doc.guideline_id},
    )
    assert all(hit["guideline_id"] != doc.guideline_id for hit in hits_excluded)


def test_article_without_psychiatry_is_excluded(db_session, qdrant, cfg):
    doc = make_doc(
        title="Statin therapy for hyperlipidaemia: a meta-analysis",
        abstract="METHODS: pooled 12 trials of statins. RESULTS: LDL fell by 40%.",
        results="LDL fell by 40%.",
        text="",
    )
    summary = pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    assert work is not None and work.status == "excluded"
    assert work.criteria["decision"] == "exclude"
    assert work.criteria["reasons"]
    assert summary.stats["excluded"] == 1
    assert summary.stats["candidates"] == 0


def test_reject_records_reason_and_removes_chunks(db_session, qdrant, cfg):
    doc = make_doc()
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    review.approve(db_session, qdrant, work.id, None, cfg)
    assert _points(qdrant, work) > 0

    review.reject(db_session, work.id, None, "Выборка не соответствует популяции", client=qdrant)
    assert work.status == "rejected"
    assert work.reject_reason.startswith("Выборка")
    assert _points(qdrant, work) == 0


def test_reject_requires_reason(db_session, qdrant, cfg):
    doc = make_doc()
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    with pytest.raises(review.ReviewError):
        review.reject(db_session, work.id, None, "  ")


def test_rerun_is_idempotent(db_session, qdrant, cfg):
    doc = make_guideline()
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    chunks = work.chunk_count
    points = _points(qdrant, work)

    second = pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    assert second.stats["unchanged"] == 1
    assert second.stats["published"] == 0
    assert work.chunk_count == chunks
    assert _points(qdrant, work) == points
    third = pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    assert third.stats["published"] == 0
    assert _points(qdrant, work) == points


def test_new_revision_replaces_old_chunks(db_session, qdrant, cfg):
    doc = make_guideline()
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    first_hash = work.indexed_hash

    updated = make_guideline(
        revision="2026-01-01#001122334455",
        text=doc.text + "\n\nНовая редакция: добавлены рекомендации по тревоге.",
        fetched_at="2026-10-21T12:00:00+00:00",
    )
    summary = pipeline.execute(db_session, qdrant, cfg, documents=[updated])
    assert summary.stats["updated"] == 1

    db_session.refresh(work)
    assert work.revision == updated.revision
    assert work.indexed_hash != first_hash
    assert len(work.revision_history) == 2
    assert work.chunk_count > 0
    assert _points(qdrant, work) == work.chunk_count


def test_source_failure_is_visible_not_silent(db_session, qdrant, cfg):
    summary = pipeline.execute(
        db_session,
        qdrant,
        cfg,
        documents=[],
        source_errors=["pubmed: HTTP 503"],
    )
    assert summary.status == "failed"
    assert summary.errors == ["pubmed: HTTP 503"]
    run = db_session.get(KBRun, uuid.UUID(summary.run_id))
    assert run is not None
    assert run.status == "failed"
    assert run.errors == ["pubmed: HTTP 503"]
    assert run.stats["fetched"] == 0


def test_offline_mode_never_calls_network(db_session, qdrant, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("offline-прогон не должен открывать сетевые соединения")

    # Патч живёт только внутри теста: teardown фикстуры снова ходит в Qdrant.
    with monkeypatch.context() as patched:
        patched.setattr(socket.socket, "connect", forbidden)
        cfg = EnrichmentConfig(offline=True)
        summary = pipeline.execute(db_session, qdrant, cfg)
    assert summary.status == "failed"
    assert any("OFFLINE" in error for error in summary.errors)


def test_run_record_has_query_plan_versions_and_stats(db_session, qdrant, cfg):
    plan = {"pubmed": {"params": {"term": "test"}}, "who": {"handles": ["10665/374250"]}}
    summary = pipeline.execute(db_session, qdrant, cfg, documents=[make_doc()], query_plan=plan)
    run = db_session.get(KBRun, uuid.UUID(summary.run_id))
    assert run is not None
    assert run.query_plan == plan
    assert run.versions["criteria"]
    assert run.versions["embedder"]["name"]
    assert run.stats["fetched"] == 1
    assert run.started_at is not None and run.finished_at is not None


def test_articles_index_has_provenance_and_abstract_only(db_session, qdrant, cfg):
    doc = make_doc()
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    review.approve(db_session, qdrant, work.id, None, cfg)
    points = qdrant.scroll(
        collection_name=kb_store.COLLECTION,
        scroll_filter=qm.Filter(
            must=[qm.FieldCondition(key="work_id", match=qm.MatchValue(value=str(work.id)))]
        ),
        limit=10,
        with_payload=True,
    )[0]
    assert points
    payload = points[0].payload
    assert payload["topic"] == ARTICLE_TOPIC
    assert payload["source"] == "pubmed"
    assert doc.url in payload["text"]
    assert "PMID" in payload["text"]


def test_rerun_keeps_approved_article_and_its_chunks(db_session, qdrant, cfg):
    doc = make_doc()
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    review.approve(db_session, qdrant, work.id, None, cfg)
    points = _points(qdrant, work)

    second = pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    db_session.refresh(work)
    assert second.stats["unchanged"] == 1
    assert work.status == "approved"
    assert _points(qdrant, work) == points


def test_rerun_keeps_rejected_article_rejected(db_session, qdrant, cfg):
    doc = make_doc()
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    review.reject(db_session, work.id, None, "Не подходит по выборке")

    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    db_session.refresh(work)
    assert work.status == "rejected"
    assert work.reject_reason.startswith("Не подходит")


def test_new_article_version_requires_new_approval(db_session, qdrant, cfg):
    doc = make_doc()
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    review.approve(db_session, qdrant, work.id, None, cfg)

    updated = make_doc(
        abstract=doc.abstract + " ADDENDUM: 3-year follow-up data added.",
        results=doc.results + " Follow-up confirmed the effect.",
        revision="2026#998877665544",
    )
    summary = pipeline.execute(db_session, qdrant, cfg, documents=[updated])
    db_session.refresh(work)
    assert summary.stats["updated"] == 1
    assert work.status == "candidate"
    assert work.approved_at is None
    assert _points(qdrant, work) == 0


def test_reindex_after_external_collection_reset(db_session, qdrant, cfg):
    """kb-seed --reset пересоздаёт коллекцию: approved-контент должен вернуться."""
    doc = make_guideline()
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    assert _points(qdrant, work) > 0

    qdrant.delete_collection(kb_store.COLLECTION)

    summary = pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    assert summary.stats["published"] == 1
    assert _points(qdrant, work) == work.chunk_count > 0


def test_proprietary_guideline_cannot_be_approved(db_session, qdrant, cfg):
    doc = make_guideline(license="proprietary")
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    assert work.status == "candidate"
    with pytest.raises(review.ReviewError):
        review.approve(db_session, qdrant, work.id, None, cfg)
    assert _points(qdrant, work) == 0


def test_unknown_cc_guideline_can_be_approved_after_manual_review(db_session, qdrant, cfg):
    doc = make_guideline(license="other-cc")
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    assert work.status == "candidate"
    review.approve(db_session, qdrant, work.id, None, cfg)
    assert work.status == "approved"
    assert _points(qdrant, work) > 0


def test_retracted_article_cannot_be_approved(db_session, qdrant, cfg):
    doc = make_doc(retraction="retracted", retraction_note="Retraction: data fabrication")
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    assert work.status == "excluded"
    with pytest.raises(review.ReviewError):
        review.approve(db_session, qdrant, work.id, None, cfg)
    assert _points(qdrant, work) == 0


def test_same_title_with_conflicting_ids_is_not_merged(db_session, qdrant, cfg):
    first = make_doc(pmid="40000001", external_id="40000001", doi="10.1/aaa")
    second = make_doc(pmid="40000002", external_id="40000002", doi="10.1/bbb")
    pipeline.execute(db_session, qdrant, cfg, documents=[second])
    summary = pipeline.execute(db_session, qdrant, cfg, documents=[first])
    works = list(db_session.scalars(select(KBWork)))
    assert summary.stats["new"] == 1
    assert summary.errors == []
    assert len(works) == 2
    assert {work.external_id for work in works} == {"40000001", "40000002"}


def _guideline_points(client, guideline_id: str) -> int:
    if not client.collection_exists(kb_store.COLLECTION):
        return 0
    return client.count(
        collection_name=kb_store.COLLECTION,
        count_filter=qm.Filter(
            must=[qm.FieldCondition(key="guideline_id", match=qm.MatchValue(value=guideline_id))]
        ),
        exact=True,
    ).count


def test_commit_failure_never_publishes_uncommitted_content(db_session, qdrant, cfg, monkeypatch):
    """Реестр коммитится до индексации: сбой коммита не оставляет чанков без work."""
    doc = make_guideline()

    original_commit = db_session.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        # 1-й commit — журнал прогона, 2-й — документ (падает), дальше — итог.
        if calls["n"] == 2:
            raise RuntimeError("commit failed")
        return original_commit()

    monkeypatch.setattr(db_session, "commit", flaky_commit)
    summary = pipeline.execute(db_session, qdrant, cfg, documents=[doc])

    assert summary.stats["published"] == 0
    assert summary.stats["failed_documents"] == 1
    assert any("commit failed" in error for error in summary.errors)
    assert _guideline_points(qdrant, doc.guideline_id) == 0
    monkeypatch.undo()
    assert db_session.scalar(
        select(KBWork).where(KBWork.guideline_id == doc.guideline_id)
    ) is None


def test_failed_reindex_restores_previous_points(db_session, qdrant, cfg, monkeypatch):
    """Неудачный upsert новой версии не должен стирать опубликованную версию."""
    doc = make_guideline()
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    assert work.status == "approved"
    before = _points(qdrant, work)
    assert before > 0

    original_upsert = qdrant.upsert
    state = {"failed": False}

    def flaky_upsert(*args, **kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("upsert down")
        return original_upsert(*args, **kwargs)

    monkeypatch.setattr(qdrant, "upsert", flaky_upsert)
    work.title = "Изменённый заголовок версии"
    with pytest.raises(RuntimeError):
        pipeline.index_mod.index_work(qdrant, HashEmbedder(), work, chunk_chars=1200)

    assert _points(qdrant, work) == before
    payloads = qdrant.scroll(
        collection_name=kb_store.COLLECTION,
        scroll_filter=qm.Filter(
            must=[qm.FieldCondition(key="work_id", match=qm.MatchValue(value=str(work.id)))]
        ),
        limit=100,
        with_payload=True,
    )[0]
    assert all("Изменённый заголовок" not in (point.payload or {}).get("text", "") for point in payloads)


def test_excluded_work_cannot_be_approved(db_session, qdrant, cfg):
    doc = make_doc(
        title="Statin therapy for hyperlipidaemia: a meta-analysis",
        abstract="METHODS: pooled 12 trials of statins. RESULTS: LDL fell by 40%.",
        results="LDL fell by 40%.",
        text="",
    )
    pipeline.execute(db_session, qdrant, cfg, documents=[doc])
    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    assert work.status == "excluded"
    with pytest.raises(review.ReviewError):
        review.approve(db_session, qdrant, work.id, None, cfg)
