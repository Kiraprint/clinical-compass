"""FR-41 (docs/18 §4.5, §8): передача подписанного протокола в ЕМИАС/РЭМД.

Три HTTP-точки:

  * `POST /clinician/sessions/{id}/protocol/submit-emias` — 202 `Delivery`
    (409 `PROTOCOL_NOT_SIGNED`, 503 `EMIAS_DISABLED`, 502 `EMIAS_UNAVAILABLE`);
  * `GET /clinician/sessions/{id}/delivery` — сводка доставки (404 до первой);
  * `POST /remd/callback` — приём `registerDocumentResult` от РЭМД, без
    клинической сессии: аутентификация — общий секрет `X-Remd-Token`
    (constant-time), идемпотентность — по `response_to_request_id`.

Права, аудит и статусная машина живут в `app.remd.service`, чтобы гейт нельзя
было обойти вторым вызовом; роутер только переводит результат в HTTP.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_clinician
from app.errors import Problem
from app.models import User
from app.remd import service as remd

router = APIRouter(tags=["session-documents"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/clinician/sessions/{session_id}/protocol/submit-emias", status_code=202)
async def submit_protocol(
    session_id: uuid.UUID,
    request: Request,
    response: Response,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Передать СЭМД в РЭМД: 202 при приёме, 200 при повторной отправке."""
    status, payload = await remd.submit(db, user.id, session_id, ip=_ip(request))
    response.status_code = status
    return payload


@router.get("/clinician/sessions/{session_id}/delivery")
async def get_delivery(
    session_id: uuid.UUID,
    user: User = Depends(require_clinician),
    db: AsyncSession = Depends(get_db),
):
    """Состояние доставки: статус, emdr_id, попытки, последняя ошибка РЭМД."""
    return await remd.get_delivery(db, user.id, session_id)


@router.post("/remd/callback")
async def remd_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """Callback РЭМД (`registerDocumentResult`). Без сессии, только по токену."""
    # Конверт callback'а невелик (emdr_id + ошибки): токен проверяем ДО чтения
    # тела, затем объявленный Content-Length и поток с жёстким потолком — лимит
    # ограничивает память, а не только разбор JSON.
    cfg = remd.check_callback_token(request.headers.get("X-Remd-Token"))
    limit = cfg.callback_max_kb * 1024
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > limit:
        raise Problem(413, "PAYLOAD_TOO_LARGE", "Тело callback больше лимита")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise Problem(413, "PAYLOAD_TOO_LARGE", "Тело callback больше лимита")
        body.extend(chunk)
    try:
        payload = json.loads(bytes(body) or b"{}")
    except ValueError:
        raise Problem(400, "VALIDATION_ERROR", "Тело callback должно быть JSON") from None
    if not isinstance(payload, dict):
        raise Problem(400, "VALIDATION_ERROR", "Тело callback должно быть объектом")
    return await remd.callback(db, payload, request.headers.get("X-Remd-Token"))
