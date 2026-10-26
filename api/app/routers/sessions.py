"""FR-38/39: календарь приёмов, клинические сессии и протокол — HTTP-слой.

Контракт: docs/18 §4.1, §4.2, §4.4 (PR P1). Здесь только ручной протокол —
загрузка аудио/расшифровка (§4.3), подпись/PDF/передача (§4.5) приходят в PR
P3/P4/P5 на те же таблицы.

Roster/согласие/IDOR-инварианты и аудит живут в сервисах
(`app.services.sessions`, `app.services.protocols`) — роутер не проверяет права
сам, чтобы один и тот же гейт нельзя было обойти из другого вызова.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.db import get_db
from app.deps import require_clinician
from app.models import User

router = APIRouter(tags=["sessions"])

Channel = Literal["in_person", "online", "phone"]
AppointmentStatus = Literal["planned", "confirmed", "completed", "cancelled", "no_show"]


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# --- §4.1 календарь ---------------------------------------------------------


class AppointmentBody(BaseModel):
    patient_id: uuid.UUID
    starts_at: datetime
    duration_min: int = Field(default=50, ge=10, le=480)
    channel: Channel = "in_person"
    location: str | None = Field(default=None, max_length=200)


class AppointmentPatch(BaseModel):
    starts_at: datetime | None = None
    duration_min: int | None = Field(default=None, ge=10, le=480)
    channel: Channel | None = None
    status: AppointmentStatus | None = None
    location: str | None = Field(default=None, max_length=200)


@router.get("/clinician/appointments")
async def list_appointments(
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    patient_id: uuid.UUID | None = Query(default=None),
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Календарь врача за окно (≤ 62 дней): свои записи, фильтр по пациенту roster."""
    return await services.sessions.list_appointments(
        db, user.id, from_, to, patient_id=patient_id
    )


@router.post("/clinician/appointments", status_code=201)
async def create_appointment(
    body: AppointmentBody,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Новая запись календаря. Пересечение у врача → 409 SLOT_CONFLICT."""
    return await services.sessions.create_appointment(
        db,
        user.id,
        body.patient_id,
        body.starts_at,
        duration_min=body.duration_min,
        channel=body.channel,
        location=body.location,
        ip=_ip(request),
    )


@router.patch("/clinician/appointments/{appointment_id}")
async def update_appointment(
    appointment_id: uuid.UUID,
    body: AppointmentPatch,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Правка записи: терминальные статусы не перепланируются (409 APPOINTMENT_NOT_ACTIVE)."""
    return await services.sessions.update_appointment(
        db, user.id, appointment_id, body.model_dump(exclude_unset=True), ip=_ip(request)
    )


@router.post("/clinician/appointments/{appointment_id}/start")
async def start_appointment(
    appointment_id: uuid.UUID,
    request: Request,
    response: Response,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Начать приём по записи: 201 — новая сессия, 200 — уже начатая (идемпотентно)."""
    payload, created = await services.sessions.start_appointment(
        db, user.id, appointment_id, ip=_ip(request)
    )
    response.status_code = 201 if created else 200
    payload["session_id"] = payload["id"]  # контракт §4.1: `{session_id}` (или 200 существующей)
    return payload


# --- §4.2 сессии ------------------------------------------------------------


class SessionBody(BaseModel):
    patient_id: uuid.UUID
    channel: Channel = "in_person"
    appointment_id: uuid.UUID | None = None
    audio_consent: bool = False


class SessionPatch(BaseModel):
    audio_consent: bool | None = None


class FinishBody(BaseModel):
    duration_min: int | None = Field(default=None, ge=1, le=480)


@router.post("/clinician/sessions", status_code=201)
async def create_session(
    body: SessionBody,
    request: Request,
    response: Response,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Приём без записи в календаре (`appointment_id=None`) или по записи."""
    payload, created = await services.sessions.create_session(
        db,
        user.id,
        body.patient_id,
        channel=body.channel,
        appointment_id=body.appointment_id,
        audio_consent=body.audio_consent,
        ip=_ip(request),
    )
    response.status_code = 201 if created else 200
    return payload


@router.get("/clinician/sessions")
async def list_sessions(
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    patient_id: uuid.UUID | None = Query(default=None),
    status: Literal["active", "finished"] | None = Query(default=None),
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Сессии врача за окно (≤ 62 дней)."""
    return await services.sessions.list_sessions(
        db, user.id, from_, to, patient_id=patient_id, status=status
    )


@router.get("/clinician/sessions/{session_id}")
async def get_session(
    session_id: uuid.UUID,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """SessionDetail: сессия + протокол + артефакты (подпись/передача — PR P4/P5)."""
    return await services.sessions.get_session(db, user.id, session_id)


@router.post("/clinician/sessions/{session_id}/finish")
async def finish_session(
    session_id: uuid.UUID,
    body: FinishBody,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Завершить сессию → ровно один приём в карточке пациента (идемпотентно)."""
    return await services.sessions.finish_session(
        db, user.id, session_id, duration_min=body.duration_min, ip=_ip(request)
    )


@router.patch("/clinician/sessions/{session_id}")
async def update_session(
    session_id: uuid.UUID,
    body: SessionPatch,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """PATCH сессии: согласие на аудиозапись (docs/18 §1.3)."""
    return await services.sessions.update_session(
        db, user.id, session_id, body.model_dump(exclude_unset=True)
    )


# --- §4.4 протокол ----------------------------------------------------------


class ProtocolBody(BaseModel):
    sections: dict
    diagnosis_code: str | None = Field(default=None, max_length=16)


@router.get("/clinician/sessions/{session_id}/protocol")
async def get_protocol(
    session_id: uuid.UUID,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Текущая версия протокола (разделы §5.1 + метаданные)."""
    return await services.protocols.get(db, user.id, session_id)


@router.put("/clinician/sessions/{session_id}/protocol")
async def put_protocol(
    session_id: uuid.UUID,
    body: ProtocolBody,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Сохранить разделы: 409 PROTOCOL_LOCKED, 422 PROTOCOL_SECTION_TOO_LONG."""
    return await services.protocols.put(
        db, user.id, session_id, body.sections, body.diagnosis_code, ip=_ip(request)
    )


@router.post("/clinician/sessions/{session_id}/protocol/approve")
async def approve_protocol(
    session_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Подтверждение врачом: 409 PROTOCOL_NOT_READY со списком пустых разделов."""
    return await services.protocols.approve(db, user.id, session_id, ip=_ip(request))
