"""FR-25: админ-контур интеграции с ЕМИАС (docs/04 §3.1).

Все эндпоинты — ADMIN (require_admin). Любой запуск синхронизации пишет
аудит EMIA_SYNC, привязка пациента — EMIA_LINK (docs/04 §5). Если интеграция
выключена (CC_EMIAS_ENABLED=false, дефолт) — 503 EMAIS_DISABLED: egress в
ЕМИАС не происходит ни при каких условиях без явного флага (docs/10 R17).

Синхронизация выполняется синхронно в запросе: объём мока — десятки записей,
фоновой очереди в системе нет. Ответ 202 содержит сохранённый прогон
emias_sync_runs (status + stats), health отдаёт последний прогон.
"""

from __future__ import annotations

import uuid
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_admin
from app.emias import sync as emias_sync
from app.emias.client import EmiasError
from app.emias.config import EmiasConfigError, current_config
from app.errors import Problem
from app.models import EmiasSyncRun, User
from app.services import audit as audit_service
router = APIRouter(prefix="/emias", tags=["admin-emias"])

class SyncBody(BaseModel):
    mode: Literal["patients", "one"] = "patients"
    patient_id: uuid.UUID | None = None

class LinkBody(BaseModel):
    patient_id: uuid.UUID
    snils: str = Field(min_length=11, max_length=20, description="СНИЛС; хранится только как sha256")

def _require_enabled():
    cfg = current_config()
    if not cfg.enabled:
        raise Problem(503, "EMIAS_DISABLED", "Интеграция с ЕМИАС выключена (CC_EMIAS_ENABLED=false)")
    try:
        cfg.validate()
    except EmiasConfigError as exc:
        raise Problem(503, "EMIAS_MISCONFIGURED", str(exc)) from None
    return cfg

def _run_dict(run: EmiasSyncRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": str(run.id),
        "trigger": run.trigger,
        "mode": run.mode,
        "status": run.status,
        "stats": run.stats,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }

@router.get("/health")
async def health(user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Состояние интеграции: флаг, хост (без секретов) и последний прогон."""
    cfg = current_config()
    last = await db.scalar(select(EmiasSyncRun).order_by(EmiasSyncRun.started_at.desc()).limit(1))
    return {
        "enabled": cfg.enabled,
        "base_host": urlparse(cfg.base_url).netloc,
        "allow_insecure": cfg.allow_insecure,
        "client_id_configured": bool(cfg.client_id and cfg.client_secret),
        "last_run": _run_dict(last),
    }

@router.get("/runs")
async def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.scalars(
        select(EmiasSyncRun).order_by(EmiasSyncRun.started_at.desc()).limit(limit)
    )).all()
    return {"items": [_run_dict(r) for r in rows]}

@router.post("/sync", status_code=202)
async def trigger_sync(
    body: SyncBody, request: Request,
    user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
):
    """Запуск синхронизации: mode=patients (обход контура) или one (patient_id)."""
    _require_enabled()
    if body.mode == "one" and body.patient_id is None:
        raise Problem(422, "EMIAS_PATIENT_REQUIRED", "Для mode=one нужен patient_id")
    ip = request.client.host if request.client else None
    await audit_service.record(db, user.id, "EMIA_SYNC", "emias_sync", None, ip=ip)
    await db.commit()
    run = await emias_sync.execute_sync(db, mode=body.mode, patient_id=body.patient_id, trigger="manual")
    return {"run": _run_dict(run)}

@router.post("/link", status_code=201)
async def link_patient(
    body: LinkBody, request: Request,
    user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
):
    """Связать локального пациента с ЕМИАС по СНИЛС (поиск Patient по identifier)."""
    _require_enabled()
    ip = request.client.host if request.client else None
    try:
        result = await emias_sync.link_patient(db, body.patient_id, body.snils)
    except EmiasError as exc:
        raise Problem(502, "EMIAS_UPSTREAM", f"ЕМИАС недоступен: {exc.code}") from None
    await audit_service.record(db, user.id, "EMIA_LINK", "patient", str(body.patient_id), ip=ip)
    await db.commit()
    return result
