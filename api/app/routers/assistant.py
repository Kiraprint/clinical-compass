# ИИ-агент врача: гибридный поиск по базе знаний + ответ с цитатами.
# Контракт: docs/04 §3.1 (расширение), docs/07, docs/08; тесты — docs/06 §3.
from __future__ import annotations

import uuid

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

_factory: AgentFactory | None = None

def _agent_factory() -> AgentFactory:
    """Один AgentFactory на процесс — граф LangGraph компилируется один раз
    (не на каждый запрос). Compiled graph.invoke потокобезопасен."""
    global _factory
    if _factory is None:
        _factory = AgentFactory(_client())
    return _factory

class AskBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    topic: str | None = Field(default=None, max_length=40)  # ручной фильтр: ШКАЛЫ|ТРИАЖ|РЕКОМЕНДАЦИИ|ЛЕКАРСТВА (иначе LLM-роутер)
    limit: int = Field(default=5, ge=1, le=20)
    patient_id: uuid.UUID | None = Field(default=None)  # @-упоминание пациента → контекст в агент

@router.post("/assistant/ask")
def ask(
    body: AskBody,
    request: Request,
    user: User = Depends(require_clinician),
    db: Session = Depends(get_db),
):
    """Справочный ответ врачу по базе знаний. Только CLINICIAN (roster не нужен:
    корпус — открытый не-ПДн контент).

    При patient_id (функция «@пациент»): загружается компактный контекст пациента
    (services.clinician.patient_context — roster-gate + согласие + audit
    ASK_ASSISTANT_WITH_PATIENT, спецкатегория ПДн 152-ФЗ) и подмешивается в ответ
    как опора; в Qdrant не попадает. Запрос без пациента аудируется ASK_ASSISTANT.
    """
    patient_ctx = None
    ip = request.client.host if request.client else None
    if body.patient_id is not None:
        patient_ctx = services.clinician.patient_context(db, user.id, body.patient_id, ip=ip)
        db.commit()  # PDn-access audit персистируется ДО вызова агента (152-ФЗ: read уже был)
    # Граница LLM (docs/12): ни запрос, ни карта не уходят наружу с ПДн.
    names = services.clinician.roster_names(db, user.id)
    query, patient_ctx, depersonalization = services.agent.scrub_for_llm(
        body.query, patient_ctx, names
    )
    result = _agent_factory().invoke(
        query, topic=body.topic, limit=body.limit, patient_context=patient_ctx
    )
    result["depersonalization"] = depersonalization
    if body.patient_id is None:
        services.audit.record(db, user.id, "ASK_ASSISTANT", "kb", object_id=None, ip=ip)
        db.commit()
    return result

@router.get("/assistant/status")
def status(user: User = Depends(require_clinician)):
    """Состояние базы знаний: число проиндексированных чанков."""
    return {"collection": settings.qdrant_collection, "points": services.kb.count_points(_client())}
