# Item 3: уведомления врача при RED-эскалации. Контракт: docs/04 §3.1.
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_consent_if_patient
from app.models import User
from app.services import push

router = APIRouter(tags=["notifications"])


class ReadBody(BaseModel):
    ids: list[str] | None = None  # None → прочитать все непрочитанные


@router.get("/me/notifications")
async def my_notifications(
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_consent_if_patient),
    db: AsyncSession = Depends(get_db),
):
    return await push.list_notifications(db, user.id, limit)


@router.post("/me/notifications/read")
async def read_notifications(
    body: ReadBody, user: User = Depends(require_consent_if_patient), db: AsyncSession = Depends(get_db)
):
    await push.mark_read(db, user.id, body.ids)
    return {"read": True}