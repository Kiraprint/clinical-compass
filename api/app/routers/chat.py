# E5: chat. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.db import get_db
from app.deps import require_consent
from app.models import User

router = APIRouter(tags=["chat"])


class ChatBody(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


@router.post("/chat/thread", status_code=201)
async def post(body: ChatBody, user: User = Depends(require_consent), db: AsyncSession = Depends(get_db)):
    return await services.chat.post_message(db, user.id, body.content)


@router.get("/chat/thread")
async def thread(request: Request, user: User = Depends(require_consent), db: AsyncSession = Depends(get_db)):
    # F6: аудит чтения пациентом своего PHI (чат)
    ip = request.client.host if request.client else None
    await services.audit.record(db, user.id, "READ_CHAT", "patient", ip=ip)
    await db.commit()  # F6: аудит чтения своего PHI должен сохраняться
    return {"items": [services.chat.to_dict(m) for m in await services.chat.list_messages(db, user.id)]}