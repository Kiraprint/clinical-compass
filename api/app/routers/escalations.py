"""Общая очередь эскалаций (docs/16 §3): видна врачу и администратору."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app import services
from app.db import get_db
from app.deps import require_roles
from app.models import User

router = APIRouter(tags=["escalations"])

_require_staff = require_roles("CLINICIAN", "ADMIN")


@router.get("/escalations")
def open_escalations(
    request: Request,
    user: User = Depends(_require_staff),
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else None
    return services.escalations.list_open(db, user.id, ip=ip)
