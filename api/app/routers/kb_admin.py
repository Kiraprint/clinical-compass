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
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import require_admin
from app.enrichment import pipeline, review
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
async def runs(limit: int = 20, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    rows = await db.scalars(select(KBRun).order_by(KBRun.started_at.desc()).limit(min(max(limit, 1), 100)))
    return {"runs": [_run_payload(run) for run in rows]}


@router.get("/kb/runs/{run_id}")
async def run_detail(run_id: uuid.UUID, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    run = await db.get(KBRun, run_id)
    if run is None:
        raise Problem(404, "NOT_FOUND", "Прогон не найден")
    return _run_payload(run)


@router.get("/kb/works")
async def works(
    status: str | None = "candidate",
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = await review.list_works(
        db,
        status=status or None,
        kind=kind,
        limit=min(max(limit, 1), 200),
        offset=max(offset, 0),
    )
    return {
        "works": [review.serialize(work) for work in rows],
        "counts": await _counts(db),
    }


@router.get("/kb/works/{work_id}")
async def work_detail(work_id: uuid.UUID, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    work = await review.get_work(db, work_id)
    if work is None:
        raise Problem(404, "NOT_FOUND", "Источник не найден")
    return review.serialize(work)


class RejectBody(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


@router.post("/kb/works/{work_id}/approve")
async def approve(work_id: uuid.UUID, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    try:
        work = await review.approve(db, _client(), work_id, user.id, EnrichmentConfig.from_env())
        await db.commit()
    except review.ReviewError as exc:
        await db.rollback()
        raise Problem(409, "REVIEW_CONFLICT", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — review.approve сам откатывает статус
        await db.rollback()
        raise Problem(503, "INDEX_FAILED", f"Индексация не завершена: {exc}") from exc
    return review.serialize(work)


@router.post("/kb/works/{work_id}/reject")
async def reject(
    work_id: uuid.UUID,
    body: RejectBody,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        work = await review.reject(db, work_id, user.id, body.reason, client=_client())
    except review.ReviewError as exc:
        await db.rollback()
        raise Problem(409, "REVIEW_CONFLICT", str(exc)) from exc
    await db.commit()
    return review.serialize(work)


@router.get("/kb/health")
async def health(user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Состояние обогащения: последний прогон, очередь кандидатов, ошибки."""
    last = await pipeline.latest_run(db)
    return {
        "last_run": _run_payload(last) if last else None,
        "counts": await _counts(db),
    }


async def _counts(db: AsyncSession) -> dict:
    rows = (await db.execute(select(KBWork.status, func.count()).group_by(KBWork.status))).all()
    return {status: int(count) for status, count in rows}
