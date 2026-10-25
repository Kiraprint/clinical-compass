"""FR-36: персональное ревью статьи врачом — видимость в выдаче ассистента.

Контракт: docs/04 FR-36, docs/07 §6–7, docs/15 §12. Требует Qdrant (иначе skip).

Проверяется продуктовый инвариант: админский approve публикует статью в индекс
глобально, но конкретный врач видит её в ответах ассистента только после СВОЕГО
decision='approved'. Гайдлайны ВОЗ (kind='guideline') видны всем без решения.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.config import settings

qdrant = pytest.importorskip("qdrant_client")

from qdrant_client import QdrantClient

from .conftest import clinician_ctx, run_async
from .enrichment_fixtures import make_doc, make_guideline

ARTICLE_QUERY = "cognitive behavioural therapy for generalised anxiety disorder"
GUIDELINE_QUERY = "mental health gap action programme depression anxiety recommendations"


def _qdrant_available() -> bool:
    try:
        QdrantClient(url=settings.qdrant_url, timeout=2).get_collections()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _qdrant_available(), reason="Qdrant недоступен (CC_QDRANT_URL)")


@pytest.fixture()
def seeded(client, db_session, monkeypatch):
    """Изолированная коллекция: опубликованная статья, кандидат и гайдлайн ВОЗ."""
    from app.enrichment import pipeline, review
    from app.enrichment.config import EnrichmentConfig
    from app.models import KBWork
    from app.services import kb as kb_store

    name = f"kb_appr_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(kb_store, "COLLECTION", name)
    qdrant_client = QdrantClient(url=settings.qdrant_url)
    cfg = EnrichmentConfig(offline=True)

    article = make_doc()
    guideline = make_guideline()
    candidate = make_doc(
        external_id="41000002",
        pmid="41000002",
        doi="10.1016/j.jad.2024.000002",
        url="https://pubmed.ncbi.nlm.nih.gov/41000002/",
        title="Antidepressant discontinuation and relapse prevention: a randomised trial",
        abstract=(
            "BACKGROUND: Relapse after antidepressant discontinuation is common. "
            "METHODS: Randomised controlled trial. "
            "RESULTS: Maintenance treatment lowered relapse risk. "
            "CONCLUSIONS: Maintenance reduces relapse."
        ),
        results="Maintenance treatment lowered relapse risk.",
    )
    docs = [article, candidate, guideline]
    run_async(pipeline.execute, qdrant_client, cfg, documents=docs)
    db_session.expire_all()
    db_session.commit()

    works = {
        work.guideline_id: work
        for work in db_session.scalars(
            select(KBWork).where(KBWork.guideline_id.in_([d.guideline_id for d in docs]))
        )
    }
    work_ids = {guideline_id: work.id for guideline_id, work in works.items()}
    # Админ публикует статью: глобальная видимость в индексе (FR-35/docs/15 §8).
    run_async(review.approve, qdrant_client, work_ids[article.guideline_id], None, cfg)
    db_session.expire_all()
    db_session.commit()

    yield {
        "article_gid": article.guideline_id,
        "article_id": work_ids[article.guideline_id],
        "candidate_gid": candidate.guideline_id,
        "candidate_id": work_ids[candidate.guideline_id],
        "guideline_gid": guideline.guideline_id,
        "guideline_id": work_ids[guideline.guideline_id],
        "client": qdrant_client,
    }
    if qdrant_client.collection_exists(name):
        qdrant_client.delete_collection(name)


def cited(client, headers, query: str) -> set[str]:
    resp = client.post(
        "/api/v1/assistant/ask", json={"query": query, "limit": 5}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return {c["guideline_id"] for c in resp.json().get("citations", [])}


def decision(client, headers, work_id, value: str, note: str | None = None):
    return client.post(
        f"/api/v1/clinician/kb/works/{work_id}/decision",
        json={"decision": value, **({"note": note} if note is not None else {})},
        headers=headers,
    )


def works(client, headers, **params) -> list[dict]:
    resp = client.get("/api/v1/clinician/kb/works", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["works"]


def test_library_row_exposes_fulltext_link(client, seeded):
    """В библиотеке у статьи есть ссылка на полный текст: в индексе только абстракт."""
    clinician, _ = clinician_ctx(client, "c-fulltext@test.ru")
    rows = works(client, clinician, status="all")
    row = next(r for r in rows if r["work_id"] == str(seeded["article_id"]))
    assert row["fulltext_url"].startswith("http")
    assert row["source_url"].startswith("http")


def test_article_requires_personal_approval(client, seeded):
    clinician, _ = clinician_ctx(client, "c-appr@test.ru")
    gid = seeded["article_gid"]
    wid = seeded["article_id"]

    # Опубликована админом, но врач её не одобрял → в ответах ассистента нет.
    assert gid not in cited(client, clinician, ARTICLE_QUERY)

    new = works(client, clinician, status="new", kind="article")
    card = next((w for w in new if w["work_id"] == str(wid)), None)
    assert card is not None
    assert card["decision"] is None and card["reviewed_at"] is None
    assert card["source"] == "pubmed" and card["year"] == 2024
    assert card["source_url"].startswith("https://pubmed.ncbi.nlm.nih.gov/")
    assert "verdicts" in card["criteria"]

    ok = decision(client, clinician, wid, "approved")
    assert ok.status_code == 200, ok.text
    assert ok.json()["decision"] == "approved"
    assert ok.json()["reviewed_at"] is not None

    # Одобрил сам → статья в выдаче.
    assert gid in cited(client, clinician, ARTICLE_QUERY)
    approved = works(client, clinician, status="approved", kind="article")
    assert [w["work_id"] for w in approved] == [str(wid)]
    assert approved[0]["decision"] == "approved"
    assert works(client, clinician, status="new", kind="article") == []

    # Передумал и отклонил → статья снова недоступна.
    bad = decision(client, clinician, wid, "rejected")
    assert bad.status_code == 200, bad.text
    assert bad.json()["decision"] == "rejected"
    assert gid not in cited(client, clinician, ARTICLE_QUERY)
    assert [w["work_id"] for w in works(client, clinician, status="rejected", kind="article")] == [
        str(wid)
    ]
    assert len(works(client, clinician, status="all", kind="article")) == 1


def test_who_guideline_visible_without_decision(client, seeded):
    clinician, _ = clinician_ctx(client, "c-who@test.ru")
    gid = seeded["guideline_gid"]

    assert gid in cited(client, clinician, GUIDELINE_QUERY)
    # Гайдлайн не требует решения и не попадает в очередь статьи-ревью.
    assert all(w["work_id"] != str(seeded["guideline_id"]) for w in works(client, clinician, status="new", kind="article"))
    all_works = works(client, clinician, status="all")
    guideline = next(w for w in all_works if w["work_id"] == str(seeded["guideline_id"]))
    assert guideline["decision"] is None


def test_other_clinician_decision_is_isolated(client, seeded):
    first, _ = clinician_ctx(client, "c-first@test.ru")
    second, _ = clinician_ctx(client, "c-second@test.ru")
    gid = seeded["article_gid"]
    wid = seeded["article_id"]

    assert decision(client, first, wid, "approved").status_code == 200

    assert gid in cited(client, first, ARTICLE_QUERY)
    # Чужое одобрение не открывает статью второму врачу.
    assert gid not in cited(client, second, ARTICLE_QUERY)
    card = next(w for w in works(client, second, status="new", kind="article") if w["work_id"] == str(wid))
    assert card["decision"] is None
    assert works(client, second, status="approved", kind="article") == []


def test_unpublished_work_cannot_be_decided(client, seeded):
    clinician, _ = clinician_ctx(client, "c-unpub@test.ru")

    resp = decision(client, clinician, seeded["candidate_id"], "approved")
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "KB_WORK_NOT_PUBLISHED"

    missing = uuid.uuid4()
    resp = decision(client, clinician, missing, "approved")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "KB_WORK_NOT_FOUND"

    # Кандидат админом не опубликован → его нет в библиотеке врача.
    assert all(w["work_id"] != str(seeded["candidate_id"]) for w in works(client, clinician, status="all"))


def test_qdrant_filter_uses_work_allow_list(seeded):
    """Фильтр в самом Qdrant-запросе: пустой allow-list скрывает статью,
    гайдлайн ВОЗ виден, а allow-list с work_id статью возвращает."""
    from app.services import kb as kb_store
    from app.services.embeddings import get_embedder

    client_q = seeded["client"]
    embedder = get_embedder()

    hidden = kb_store.hybrid_search(
        client_q, embedder, ARTICLE_QUERY, limit=10, work_ids=set()
    )
    assert hidden and all(h["guideline_id"] != seeded["article_gid"] for h in hidden)
    assert any(h["kind"] == "guideline" for h in hidden)

    shown = kb_store.hybrid_search(
        client_q,
        embedder,
        ARTICLE_QUERY,
        limit=10,
        work_ids={str(seeded["article_id"])},
    )
    assert any(h["guideline_id"] == seeded["article_gid"] for h in shown)

    # Seed-корпус (payload без work_id) виден при пустом allow-list: иначе
    # персональный фильтр вырезал бы шкалы/препараты из ответов всем врачам.
    kb_store.upsert_documents(
        client_q,
        embedder,
        [
            {
                "guideline_id": "scale-phq9-seed",
                "source_title": "Шкала PHQ-9 — депрессия",
                "topic": "ШКАЛЫ",
                "text": "PHQ-9: шкала оценки тяжести депрессии, девять пунктов.",
            }
        ],
    )
    seed_hits = kb_store.hybrid_search(
        client_q, embedder, "шкала PHQ-9 депрессия", limit=10, work_ids=set()
    )
    assert any(h["guideline_id"] == "scale-phq9-seed" for h in seed_hits)

    # None — служебный путь без персонального фильтра.
    unfiltered = kb_store.hybrid_search(client_q, embedder, ARTICLE_QUERY, limit=10)
    assert any(h["guideline_id"] == seeded["article_gid"] for h in unfiltered)


def test_decision_is_idempotent(client, db_session, seeded):
    from app.models import AuditLog

    clinician, clinician_id = clinician_ctx(client, "c-idem@test.ru")
    wid = seeded["article_id"]

    first = decision(client, clinician, wid, "approved", note="обзор качественный")
    assert first.status_code == 200, first.text
    reviewed_at = first.json()["reviewed_at"]

    second = decision(client, clinician, wid, "approved")
    assert second.status_code == 200, second.text
    assert second.json()["reviewed_at"] == reviewed_at

    db_session.expire_all()
    db_session.commit()
    audits = db_session.scalars(
        select(AuditLog).where(
            AuditLog.action == "KB_WORK_REVIEW",
            AuditLog.object_id == str(wid),
        )
    ).all()
    # Один аудит на фактическое решение; повторный клик не пишет журнал.
    assert len(audits) == 1
    assert audits[0].actor_id == uuid.UUID(clinician_id)
    assert audits[0].object_type == "kb_work"

    # Смена решения — новое reviewed_at и вторая запись аудита.
    third = decision(client, clinician, wid, "rejected")
    assert third.status_code == 200
    assert third.json()["reviewed_at"] != reviewed_at
    db_session.expire_all()
    db_session.commit()
    assert len(
        db_session.scalars(
            select(AuditLog).where(
                AuditLog.action == "KB_WORK_REVIEW",
                AuditLog.object_id == str(wid),
            )
        ).all()
    ) == 2


def test_note_is_not_persisted_as_text(client, db_session, seeded):
    """Свободный текст заметки не хранится (152-ФЗ) — в журнале только work_id."""
    from app.models import AuditLog

    clinician, _ = clinician_ctx(client, "c-note@test.ru")
    wid = seeded["article_id"]
    note = "Пациент Петров Иван, упоминание в заметке"
    assert decision(client, clinician, wid, "rejected", note=note).status_code == 200

    db_session.expire_all()
    db_session.commit()
    rows = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "KB_WORK_REVIEW", AuditLog.object_id == str(wid))
    ).all()
    assert rows and all(note not in str(row.object_id) for row in rows)

    too_long = decision(client, clinician, wid, "approved", note="x" * 501)
    assert too_long.status_code == 422


def test_visibility_helper_rules():
    """Юнит-правило видимости без Qdrant: seed/гайдлайны всегда, статьи — по allow-list."""
    from app.services.kb import is_visible

    assert is_visible({"guideline_id": "drug-1"}, set()) is True  # seed без work_id
    assert is_visible({"work_id": "w-1", "kind": "article"}, set()) is False
    assert is_visible({"work_id": "w-1", "kind": "article"}, {"w-1"}) is True
    assert is_visible({"work_id": "w-2", "kind": "guideline"}, set()) is True
    assert is_visible({"work_id": "w-1", "kind": "article"}, None) is True
