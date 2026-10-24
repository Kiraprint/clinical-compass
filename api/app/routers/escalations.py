"""Общая очередь эскалаций (docs/16 §3): видна врачу и администратору, закрывается ими же."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.db import get_db
from app.deps import require_roles
from app.models import User

router = APIRouter(tags=["escalations"])

_require_staff = require_roles("CLINICIAN", "ADMIN")


class ResolveBody(BaseModel):
    # Длину проверяет сервис (RESOLUTION_TOO_LONG), а не pydantic: иначе клиент
    # получает 422 VALIDATION вместо документированного кода.
    resolution: str | None = None


@router.get("/escalations")
async def queue_escalations(
    request: Request,
    status: Literal["open", "resolved", "all"] = Query("open"),
    limit: int = Query(
        services.escalations.DEFAULT_LIMIT, ge=1, le=services.escalations.MAX_LIMIT
    ),
    user: User = Depends(_require_staff),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    ip = request.client.host if request.client else None
    return await services.escalations.list_queue(
        db, user.id, ip=ip, status=status, limit=limit, is_admin=user.role == "ADMIN"
    )


@router.post("/escalations/{escalation_id}/resolve")
async def resolve_escalation(
    escalation_id: str,
    body: ResolveBody,
    request: Request,
    user: User = Depends(_require_staff),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    ip = request.client.host if request.client else None
    return await services.escalations.resolve(db, user.id, escalation_id, body.resolution, ip)
