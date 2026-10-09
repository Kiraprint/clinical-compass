# E5: chat. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import services
from app.db import get_db
from app.deps import require_consent
from app.models import User

router = APIRouter(tags=["chat"])


class ChatBody(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


@router.post("/chat/thread", status_code=201)
def post(body: ChatBody, user: User = Depends(require_consent), db: Session = Depends(get_db)):
    return services.chat.post_message(db, user.id, body.content)


@router.get("/chat/thread")
def thread(user: User = Depends(require_consent), db: Session = Depends(get_db)):
    return {"items": [services.chat.to_dict(m) for m in services.chat.list_messages(db, user.id)]}