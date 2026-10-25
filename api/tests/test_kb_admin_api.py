"""API ревью КБ и персональных исключений: RBAC, approve/reject, фильтр выдачи."""

from __future__ import annotations

import uuid

import pytest

from app.config import settings

qdrant = pytest.importorskip("qdrant_client")

from qdrant_client import QdrantClient

from .conftest import admin_ctx, clinician_ctx, patient_ctx, run_async
from .enrichment_fixtures import make_doc


def _qdrant_available() -> bool:
    try:
        QdrantClient(url=settings.qdrant_url, timeout=2).get_collections()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _qdrant_available(), reason="Qdrant недоступен (CC_QDRANT_URL)")


@pytest.fixture()
def seeded(client, db_session, monkeypatch):
    """Одна статья-кандидат в изолированной коллекции + одобрена для exclusion-тестов."""
    from app.enrichment import pipeline, review
    from app.enrichment.config import EnrichmentConfig
    from app.services import kb as kb_store

    name = f"kb_api_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(kb_store, "COLLECTION", name)
    qdrant_client = QdrantClient(url=settings.qdrant_url)
    cfg = EnrichmentConfig(offline=True)
    doc = make_doc()

    run_async(pipeline.execute, qdrant_client, cfg, documents=[doc])
    db_session.expire_all()
    from sqlalchemy import select

    from app.models import KBWork

    work = db_session.scalar(select(KBWork).where(KBWork.guideline_id == doc.guideline_id))
    db_session.commit()
    yield {"work": work, "cfg": cfg, "client": qdrant_client, "doc": doc}
    if qdrant_client.collection_exists(name):
        qdrant_client.delete_collection(name)


def _work_id(work) -> str:
    return str(work.id)


def test_admin_endpoints_require_admin(client):
    clinician, _ = clinician_ctx(client, "c-kb@test.ru")
    patient, _ = patient_ctx(client, "p-kb@test.ru")
    for headers in (clinician, patient):
        for path in ("/api/v1/admin/kb/runs", "/api/v1/admin/kb/works", "/api/v1/admin/kb/health"):
            assert client.get(path, headers=headers).status_code == 403


def test_admin_lists_runs_and_candidates(client, db_session, seeded):
    admin, _ = admin_ctx(client, "a-kb@test.ru")
    works = client.get("/api/v1/admin/kb/works?status=candidate", headers=admin)
    assert works.status_code == 200
    body = works.json()
    assert any(item["guideline_id"] == seeded["doc"].guideline_id for item in body["works"])
    assert body["counts"]["candidate"] >= 1

    runs = client.get("/api/v1/admin/kb/runs", headers=admin)
    assert runs.status_code == 200
    assert runs.json()["runs"][0]["versions"]["criteria"]

    health = client.get("/api/v1/admin/kb/health", headers=admin)
    assert health.status_code == 200
    assert health.json()["last_run"]["status"] == "ok"


def test_admin_approve_publishes_to_qdrant(client, db_session, seeded):
    admin, _ = admin_ctx(client, "a-approve@test.ru")
    work_id = _work_id(seeded["work"])
    resp = client.post(f"/api/v1/admin/kb/works/{work_id}/approve", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["indexed"] is True
    assert body["chunk_count"] > 0
    assert body["criteria"]["decision"] == "include"
    assert body["appraisal"]["tool"] == "amstar2"

    again = client.post(f"/api/v1/admin/kb/works/{work_id}/approve", headers=admin)
    assert again.status_code == 409
    assert again.json()["code"] == "REVIEW_CONFLICT"


def test_admin_reject_requires_reason_and_records_it(client, db_session, seeded):
    admin, _ = admin_ctx(client, "a-reject@test.ru")
    work_id = _work_id(seeded["work"])
    short = client.post(
        f"/api/v1/admin/kb/works/{work_id}/reject", json={"reason": "x"}, headers=admin
    )
    assert short.status_code == 422

    ok = client.post(
        f"/api/v1/admin/kb/works/{work_id}/reject",
        json={"reason": "Не та популяция (дети)"},
        headers=admin,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "rejected"
    assert ok.json()["reject_reason"].startswith("Не та популяция")


def test_clinician_can_hide_and_restore_source(client, db_session, seeded):
    from app.enrichment import review

    clinician, _ = clinician_ctx(client, "c-excl@test.ru")
    work = seeded["work"]
    run_async(review.approve, seeded["client"], work.id, None, seeded["cfg"])
    db_session.expire_all()
    db_session.commit()

    sources = client.get("/api/v1/assistant/sources", headers=clinician)
    assert sources.status_code == 200
    item = next(s for s in sources.json()["sources"] if s["guideline_id"] == work.guideline_id)
    assert item["excluded"] is False

    hide = client.post(
        f"/api/v1/assistant/sources/{work.guideline_id}/exclude",
        json={"excluded": True},
        headers=clinician,
    )
    assert hide.status_code == 200
    assert work.guideline_id in hide.json()["excluded_guideline_ids"]

    sources = client.get("/api/v1/assistant/sources", headers=clinician)
    item = next(s for s in sources.json()["sources"] if s["guideline_id"] == work.guideline_id)
    assert item["excluded"] is True

    restore = client.post(
        f"/api/v1/assistant/sources/{work.guideline_id}/exclude",
        json={"excluded": False},
        headers=clinician,
    )
    assert restore.status_code == 200
    assert restore.json()["excluded_guideline_ids"] == []


def test_unknown_source_cannot_be_excluded(client, db_session, seeded):
    clinician, _ = clinician_ctx(client, "c-unknown@test.ru")
    resp = client.post(
        "/api/v1/assistant/sources/who-000000000/exclude",
        json={"excluded": True},
        headers=clinician,
    )
    assert resp.status_code == 404


def test_assistant_retrieval_respects_personal_exclusion(client, db_session, seeded):
    from app.enrichment import review

    clinician, _ = clinician_ctx(client, "c-ask@test.ru")
    work = seeded["work"]
    run_async(review.approve, seeded["client"], work.id, None, seeded["cfg"])
    db_session.expire_all()
    db_session.commit()

    # FR-36: публикации админом мало — статья видна врачу только после его
    # собственного одобрения (персональная видимость, не публикация).
    approved = client.post(
        f"/api/v1/clinician/kb/works/{work.id}/decision",
        json={"decision": "approved"},
        headers=clinician,
    )
    assert approved.status_code == 200, approved.text

    body = {"query": "cognitive behavioural therapy generalised anxiety", "limit": 5}
    before = client.post("/api/v1/assistant/ask", json=body, headers=clinician)
    assert before.status_code == 200, before.text
    cited = {c["guideline_id"] for c in before.json().get("citations", [])}
    assert work.guideline_id in cited

    client.post(
        f"/api/v1/assistant/sources/{work.guideline_id}/exclude",
        json={"excluded": True},
        headers=clinician,
    )
    after = client.post("/api/v1/assistant/ask", json=body, headers=clinician)
    assert after.status_code == 200, after.text
    cited_after = {c["guideline_id"] for c in after.json().get("citations", [])}
    assert work.guideline_id not in cited_after
