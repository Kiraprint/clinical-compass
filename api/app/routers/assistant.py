# ИИ-агент врача: гибридный поиск по базе знаний + ответ с цитатами.
# Контракт: docs/04 §3.1 (расширение), docs/07, docs/08; тесты — docs/06 §3.
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from app import services
from app.config import settings
from app.db import get_db
from app.deps import require_clinician
from app.models import User
from app.services.agent import AgentFactory

router = APIRouter(tags=["assistant"])

_qdrant: QdrantClient | None = None

def _client() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = QdrantClient(url=settings.qdrant_url)
    return _qdrant

class AskBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    topic: str | None = Field(default=None, max_length=40)  # фильтр: ШКАЛЫ|ТРИАЖ|РЕКОМЕНДАЦИИ|ПРЕПАРАТЫ
    limit: int = Field(default=5, ge=1, le=20)

@router.post("/assistant/ask")
def ask(
    body: AskBody,
    request: Request,
    user: User = Depends(require_clinician),
    db: Session = Depends(get_db),
):
    """Справочный ответ врачу по базе знаний. Только CLINICIAN (roster не нужен:
    корпус — открытый не-ПДн контент, связи с конкретным пациентом нет).

    Каждый запрос аудируется (спец-категория не затрагивается, но фиксируем
    обращение врача к ассистенту — docs/04 §5).
    """
    result = AgentFactory(_client()).invoke(body.query, topic=body.topic, limit=body.limit)
    ip = request.client.host if request.client else None
    services.audit.record(db, user.id, "ASK_ASSISTANT", "kb", object_id=None, ip=ip)
    db.commit()
    return result

@router.get("/assistant/status")
def status(user: User = Depends(require_clinician)):
    """Состояние базы знаний: число проиндексированных чанков."""
    return {"collection": settings.qdrant_collection, "points": services.kb.count_points(_client())}
