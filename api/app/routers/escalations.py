"""Общая очередь эскалаций (docs/16 §3): видна врачу и администратору, закрывается ими же."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import services
from app.db import get_db
from app.deps import require_roles
from app.models import User

router = APIRouter(tags=["escalations"])

_require_staff = require_roles("CLINICIAN", "ADMIN")


class ResolveBody(BaseModel):
    resolution: str | None = Field(default=None, max_length=1000)


@router.get("/escalations")
def open_escalations(
    request: Request,
    limit: int = Query(
        services.escalations.DEFAULT_LIMIT, ge=1, le=services.escalations.MAX_LIMIT
    ),
    user: User = Depends(_require_staff),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ip = request.client.host if request.client else None
    return services.escalations.list_open(
        db, user.id, ip=ip, limit=limit, is_admin=user.role == "ADMIN"
    )


@router.post("/escalations/{escalation_id}/resolve")
def resolve_escalation(
    escalation_id: uuid.UUID,
    body: ResolveBody,
    request: Request,
    user: User = Depends(_require_staff),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ip = request.client.host if request.client else None
    return services.escalations.resolve(db, user.id, escalation_id, body.resolution, ip)
