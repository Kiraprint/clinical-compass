"""Админ-ревью обогащения КБ: прогоны, источники, approve/reject (docs/15).

Доступ — только ADMIN (как остальные /admin). Приёмка статьи в индекс —
единственное место, где клинический контент попадает в Qdrant по решению
человека; гайдлайны ВОЗ публикует сервис kb-enrichment автоматически.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import require_admin
from app.enrichment import index as kb_index, pipeline, review
from app.enrichment.config import EnrichmentConfig
from app.errors import Problem
from app.models import KBRun, KBWork, User

router = APIRouter(tags=["admin-kb"])

_qdrant: QdrantClient | None = None


def _client() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = QdrantClient(url=settings.qdrant_url)
    return _qdrant


def _run_payload(run: KBRun) -> dict:
    return {
        "id": str(run.id),
        "trigger": run.trigger,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "stats": run.stats,
        "errors": run.errors,
        "query_plan": run.query_plan,
        "versions": run.versions,
    }


@router.get("/kb/runs")
def runs(limit: int = 20, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(KBRun).order_by(KBRun.started_at.desc()).limit(min(max(limit, 1), 100)))
    return {"runs": [_run_payload(run) for run in rows]}


@router.get("/kb/runs/{run_id}")
def run_detail(run_id: uuid.UUID, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    run = db.get(KBRun, run_id)
    if run is None:
        raise Problem(404, "NOT_FOUND", "Прогон не найден")
    return _run_payload(run)


@router.get("/kb/works")
def works(
    status: str | None = "candidate",
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = review.list_works(
        db,
        status=status or None,
        kind=kind,
        limit=min(max(limit, 1), 200),
        offset=max(offset, 0),
    )
    return {
        "works": [review.serialize(work) for work in rows],
        "counts": _counts(db),
    }


@router.get("/kb/works/{work_id}")
def work_detail(work_id: uuid.UUID, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    work = review.get_work(db, work_id)
    if work is None:
        raise Problem(404, "NOT_FOUND", "Источник не найден")
    return review.serialize(work)


class RejectBody(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


@router.post("/kb/works/{work_id}/approve")
def approve(work_id: uuid.UUID, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        work = review.approve(db, _client(), work_id, user.id, EnrichmentConfig.from_env())
        db.commit()
    except review.ReviewError as exc:
        db.rollback()
        raise Problem(409, "REVIEW_CONFLICT", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — индекс уже записан, БД откатилась
        db.rollback()
        try:
            kb_index.remove_work_id(_client(), work_id)
        except Exception:  # noqa: BLE001 — уборка best effort
            pass
        raise Problem(503, "INDEX_FAILED", f"Индексация не завершена: {exc}") from exc
    return review.serialize(work)


@router.post("/kb/works/{work_id}/reject")
def reject(
    work_id: uuid.UUID,
    body: RejectBody,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        work = review.reject(db, work_id, user.id, body.reason, client=_client())
    except review.ReviewError as exc:
        db.rollback()
        raise Problem(409, "REVIEW_CONFLICT", str(exc)) from exc
    db.commit()
    return review.serialize(work)


@router.get("/kb/health")
def health(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Состояние обогащения: последний прогон, очередь кандидатов, ошибки."""
    last = pipeline.latest_run(db)
    return {
        "last_run": _run_payload(last) if last else None,
        "counts": _counts(db),
    }


def _counts(db: Session) -> dict:
    rows = db.execute(select(KBWork.status, func.count()).group_by(KBWork.status)).all()
    return {status: int(count) for status, count in rows}
