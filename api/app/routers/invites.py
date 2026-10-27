"""FR-49: приглашение пациента врачом — ссылка вместо ручного админа.

Контракт: docs/04 §3.1 (роуты), docs/03 §16 (схема и правила), тесты —
docs/06 §3. Ссылку видно один раз (в БД только хеш токена), поэтому вместо
повторного показа есть перевыпуск.
"""

from __future__ import annotations

import uuid
from datetime import date

import anyio
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import security
from app.db import get_db
from app.deps import require_clinician
from app.models import PatientInvite, User
from app.routers.auth import issue_session
from app.services import invites as invites_service
from app.services.contacts import mask_phone

router = APIRouter(prefix="/clinician/patients/invites", tags=["invites"])
public = APIRouter(prefix="/auth/invite", tags=["invites"])


class InviteBody(BaseModel):
    fio: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    birth_date: date | None = None
    email: str | None = Field(default=None, max_length=255)


class AcceptBody(BaseModel):
    # E-mail нужен только если врач его не заполнил: он становится логином.
    email: str | None = Field(default=None, min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _link(token: str) -> str:
    """Относительный путь: origin подставит интерфейс (и стенд, и прод)."""
    return invites_service.INVITE_PATH_PREFIX + token


def _invite_row(invite: PatientInvite) -> dict:
    return {
        "id": str(invite.id),
        "fio": invite.fio,
        "phone": mask_phone(invite.phone),
        "birth_date": invite.birth_date.isoformat() if invite.birth_date else None,
        "email": invite.email,
        "expires_at": invite.expires_at.isoformat(),
        "created_at": invite.created_at.isoformat() if invite.created_at else None,
    }


@router.post("", status_code=201)
async def create_invite(
    body: InviteBody,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Создать карту-приглашение и получить ссылку (единственный раз)."""
    invite, token = await invites_service.create(
        db,
        user.id,
        fio=body.fio,
        phone=body.phone,
        birth_date=body.birth_date,
        email=body.email,
        ip=_ip(request),
    )
    return {**_invite_row(invite), "token": token, "link": _link(token)}


@router.get("")
async def list_invites(
    user: User = Depends(require_clinician), db: AsyncSession = Depends(get_db)
):
    rows = await invites_service.list_pending(db, user.id)
    return {"items": [_invite_row(row) for row in rows]}


@router.post("/{invite_id}/reissue", status_code=201)
async def reissue_invite(
    invite_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    invite, token = await invites_service.reissue(db, user.id, invite_id, ip=_ip(request))
    return {**_invite_row(invite), "token": token, "link": _link(token)}


@router.delete("/{invite_id}", status_code=204)
async def revoke_invite(
    invite_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    await invites_service.revoke(db, user.id, invite_id, ip=_ip(request))
    return Response(status_code=204)


@public.get("/{token}")
async def read_invite(token: str, db: AsyncSession = Depends(get_db)):
    """Публично: что за карта и какой врач приглашает (без ПДн сверх ФИО)."""
    invite = await invites_service.load(db, token)
    clinician = await db.get(User, invite.clinician_id)
    return {
        "fio": invite.fio,
        "email": invite.email,
        "clinician_fio": (clinician.fio if clinician else None),
        "expires_at": invite.expires_at.isoformat(),
    }


@public.post("/{token}/accept", status_code=201)
async def accept_invite(
    token: str,
    body: AcceptBody,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Активация: аккаунт пациента + закрепление за врачом + сессия.

    Строку приглашения держим под FOR UPDATE: два одновременных клика по
    одной ссылке иначе создали бы два аккаунта на одну карту.
    """
    invite = await invites_service.load(db, token, lock=True)
    # argon2id — десятки миллисекунд и 64 МиБ: считаем в потоке, не на loop.
    password_hash = await anyio.to_thread.run_sync(security.hash_password, body.password)
    user = await invites_service.accept(
        db, invite, email=body.email, password_hash=password_hash, ip=_ip(request)
    )
    return await issue_session(db, user, response)
