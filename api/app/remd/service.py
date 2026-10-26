"""FR-41 (docs/18 §4.5, §8): передача подписанного СЭМД в РЭМД/ЕМИАС.

Оркестрация трёх действий поверх клиента РЭМД:

1. **submit** — только подписанный протокол; конверт собирается из байт СЭМД
   (`document_artifacts(kind='protocol_cda')`) и отсоединённой CMS-подписи
   (`kind='signature'`), поэтому PDF и PNG в РЭМД не уходят. Строка доставки
   блокируется вместе с версией протокола: два одновременных submit дают один
   `request_id` и одну запись реестра.
2. **callback** — аутентифицированный приём `registerDocumentResult`;
   upsert по `response_to_request_id` (at-least-once), терминальные статусы не
   перезаписываются.
3. **delivery** — сводка для `SessionDetail` и `GET …/delivery`.

Контракт §8: `queued → sending → registered | rejected | failed`; `registered`
и `rejected` терминальны (`rejected` — РЭМД сказал «нет», новый контент = новая
версия протокола), `failed` — транспортная ошибка после всех ретраев, её можно
повторить тем же `local_document_uid`. Повторный submit уже зарегистрированной
версии возвращает существующую доставку, второй регистрации не происходит.

СНИЛС/ФИО и содержимое СЭМД не попадают ни в логи, ни в ответы API.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import uuid
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.errors import Problem
from app.models import (
    ClinicalSession,
    DocumentArtifact,
    EmiasDelivery,
    ProtocolSignature,
    SessionProtocol,
    User,
)
from app.services import artifacts, audit, sessions

from . import mapping
from .client import RemdClient, RemdRejected
from .config import RemdConfig

log = logging.getLogger(__name__)

SIGNATURE_ROLE_PRIORITY = ("clinician", "mo")
# Сколько ждать `request_id`, если по этой версии уже идёт отправка (ответ без гонки).
_REQUEST_ID_WAIT_SEC = 5.0


async def _signed_protocol(
    db: AsyncSession, session_id, *, lock: bool = False
) -> SessionProtocol | None:
    """Последняя подписанная версия сессии (признак — строки подписи).

    Та же выборка, что в `app.services.signing`, но с `FOR UPDATE` на строке
    версии: передача и правка не должны пересекаться.
    """
    stmt = (
        select(SessionProtocol)
        .join(ProtocolSignature, ProtocolSignature.protocol_id == SessionProtocol.id)
        .where(SessionProtocol.session_id == session_id)
        .order_by(SessionProtocol.version.desc())
        .limit(1)
    )
    if lock:
        stmt = stmt.with_for_update(of=SessionProtocol)
    return await db.scalar(stmt)


async def _latest_artifact(db: AsyncSession, protocol_id, kind: str) -> DocumentArtifact | None:
    return await db.scalar(
        select(DocumentArtifact)
        .where(DocumentArtifact.protocol_id == protocol_id, DocumentArtifact.kind == kind)
        .order_by(DocumentArtifact.created_at.desc())
        .limit(1)
    )


async def _prepared(
    db: AsyncSession, session: ClinicalSession, protocol: SessionProtocol
) -> tuple[bytes, ProtocolSignature, bytes]:
    """Байты СЭМД, подпись основного подписанта и DER контейнера."""
    cda_row = await _latest_artifact(db, protocol.id, "protocol_cda")
    if cda_row is None:
        raise Problem(409, "PROTOCOL_NOT_SIGNED", "Байты СЭМД не найдены у протокола")
    rows = (
        await db.scalars(
            select(ProtocolSignature).where(ProtocolSignature.protocol_id == protocol.id)
        )
    ).all()
    if not rows:
        raise Problem(409, "PROTOCOL_NOT_SIGNED", "Протокол не подписан")
    by_role = {row.role: row for row in rows}
    signature = next(
        (by_role[role] for role in SIGNATURE_ROLE_PRIORITY if role in by_role), rows[0]
    )
    cms_artifact = await db.get(DocumentArtifact, signature.cms_artifact_id)
    if cms_artifact is None:
        raise Problem(409, "PROTOCOL_NOT_SIGNED", "Контейнер подписи не найден")
    return artifacts.get_bytes(cda_row), signature, artifacts.get_bytes(cms_artifact)


def _personals(rows) -> list[dict]:
    out = []
    for row in rows:
        surname, name, patronymic = mapping.split_fio(row.fio)
        out.append(
            {
                "local_id": str(row.signer_user_id or f"mo:{row.role}"),
                "surname": surname,
                "name": name,
                "patr_name": patronymic,
                "role": row.role,
            }
        )
    return out


async def _protocol_rows(db: AsyncSession, protocol_id) -> list[ProtocolSignature]:
    return list(
        (
            await db.scalars(
                select(ProtocolSignature)
                .where(ProtocolSignature.protocol_id == protocol_id)
                .order_by(ProtocolSignature.role)
            )
        ).all()
    )


def delivery_payload(row: EmiasDelivery | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": str(row.id),
        "status": row.status,
        "external_id": row.external_id,
        "request_id": row.request_id,
        "attempts": row.attempts,
        "last_error_code": row.last_error_code,
        "last_error_message": row.last_error_message,
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "registered_at": row.registered_at.isoformat() if row.registered_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def latest_delivery(db: AsyncSession, session_id) -> EmiasDelivery | None:
    return await db.scalar(
        select(EmiasDelivery)
        .where(EmiasDelivery.session_id == session_id)
        .order_by(EmiasDelivery.submitted_at.desc())
        .limit(1)
    )


# --- отправка ----------------------------------------------------------------


def _age_seconds(row: EmiasDelivery, now) -> float:
    """Сколько строка живёт в текущем статусе (по submitted_at/updated_at)."""
    since = row.submitted_at or row.updated_at
    if since is None:
        return float("inf")
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return (now - since).total_seconds()


async def _lock_delivery(db: AsyncSession, delivery_id) -> EmiasDelivery:
    """Строка доставки под FOR UPDATE: короткая транзакция записи результата."""
    row = await db.scalar(
        select(EmiasDelivery)
        .where(EmiasDelivery.id == delivery_id)
        .with_for_update()
    )
    assert row is not None  # строка создана/найдена этим же submit
    return row


async def _mark_registered(db: AsyncSession, row: EmiasDelivery) -> None:
    """Перевести доставку и версию протокола в «зарегистрировано»."""
    row.status = "registered"
    row.registered_at = mapping.utcnow()
    protocol = await db.get(SessionProtocol, row.protocol_id)
    if protocol is not None and protocol.status == "signed":
        protocol.status = "submitted"


async def submit(
    db: AsyncSession,
    clinician_id,
    session_id,
    ip: str | None = None,
    *,
    client: RemdClient | None = None,
) -> tuple[int, dict]:
    """Отправить подписанный протокол. Возвращает (HTTP-код, Delivery).

    Блокировка версии протокола держится только на решение о состоянии
    (создать/взять строку доставки), затем коммит: сетевой вызов РЭМД идёт вне
    транзакции, а результат пишется коротким `FOR UPDATE` по строке доставки.
    """
    cfg = RemdConfig.from_settings()
    if not cfg.enabled:
        raise Problem(
            503, "EMIAS_DISABLED", "Передача в ЕМИАС выключена (CC_REMD_ENABLED=false)"
        )
    session = await sessions.require_session(db, clinician_id, session_id)
    await sessions.require_patient_access(db, clinician_id, session.patient_id)
    now = mapping.utcnow()

    protocol = await _signed_protocol(db, session.id, lock=True)
    if protocol is None:
        raise Problem(409, "PROTOCOL_NOT_SIGNED", "Передавать можно только подписанный протокол")

    row = await db.scalar(
        select(EmiasDelivery).where(EmiasDelivery.protocol_id == protocol.id)
    )
    if row is not None and row.status in mapping.TERMINAL_STATUSES:
        # Повтор уже зарегистрированной/отклонённой версии: второй регистрации нет.
        await db.commit()
        return 200, delivery_payload(row)

    if row is not None and row.status == "sending":
        if _age_seconds(row, now) < cfg.sending_stale_sec:
            # Свежий `sending`: callback ещё может прийти, второй регистрации нет.
            # Запрос-конкурент отдаёт `request_id`, когда первая отправка его допишет:
            # контракт ответа не должен зависеть от гонки (иначе клиент видит `null`).
            await db.commit()
            deadline = asyncio.get_running_loop().time() + _REQUEST_ID_WAIT_SEC
            while (
                row.request_id is None
                and row.status == "sending"
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.02)
                await db.refresh(row)
            return 202, delivery_payload(row)
        # Callback потерян. Фиксируем это как `failed`, а не как отправку: РЭМД ничего
        # не подтвердил, и повторный клик зарегистрирует документ заново тем же UID.
        row.status = "failed"
        row.last_error_code = "CALLBACK_TIMEOUT"
        row.last_error_message = "Callback РЭМД не пришёл за отведённое время"
        row.result = {"status": "error", "error_code": "CALLBACK_TIMEOUT"}
        await audit.record(db, clinician_id, "EMIAS_SUBMIT", "session", str(session.id), ip=ip)
        await db.commit()
        await db.refresh(row)
        log.info("REMd protocol %s: callback timeout", protocol.id)
        return 202, delivery_payload(row)

    if row is not None:
        # `queued`/`failed` — повторная отправка тем же local_document_uid.
        row.status = "sending"
        row.attempts += 1
        row.submitted_at = now
        row.last_error_code = None
        row.last_error_message = None

    cda, signature, signature_der = await _prepared(db, session, protocol)
    patient = await db.get(User, session.patient_id)
    envelope = mapping.build_envelope(
        cfg=cfg,
        protocol_id=protocol.id,
        version=protocol.version,
        document_number_value=mapping.document_number(protocol.id, protocol.version),
        cda=cda,
        signature_der=signature_der,
        patient_fio=patient.fio if patient else None,
        patient_birth_date=patient.birth_date if patient else None,
        patient_snils=None,
        personals=_personals(await _protocol_rows(db, protocol.id)),
    )
    digest = hashlib.sha256(cda).hexdigest()
    if row is None:
        row = EmiasDelivery(
            session_id=session.id,
            protocol_id=protocol.id,
            status="sending",
            local_document_uid=mapping.local_document_uid(protocol.id, protocol.version),
            document_sha256=digest,
            kind=cfg.document_kind,
            attempts=1,
            submitted_at=now,
        )
        db.add(row)
        await db.flush()
    else:
        row.document_sha256 = digest
        row.kind = cfg.document_kind
    await db.commit()  # блокировка версии снята до сети

    remd = client or RemdClient(cfg)
    try:
        request_id = await run_in_threadpool(remd.register_document, envelope)
    except RemdRejected as exc:
        row = await _lock_delivery(db, row.id)
        if row.status not in mapping.TERMINAL_STATUSES:
            # 403 (scope/конфигурация) и коды «повтор: да» — не приговор документу:
            # это сбой контура, его лечит повтор тем же local_document_uid.
            retryable = exc.status == 403 or exc.code in mapping.RETRYABLE_ERROR_CODES
            row.status = "failed" if retryable else "rejected"
            row.last_error_code = exc.code
            row.last_error_message = exc.message
            row.result = {
                "message_type": "registerDocument",
                "status": "error",
                "errors": [{"code": exc.code, "message": exc.message}],
            }
        await audit.record(
            db, clinician_id, "EMIAS_SUBMIT", "session", str(session.id), ip=ip
        )
        await db.commit()
        log.info("REMd rejected protocol %s: %s (%s)", protocol.id, exc.code, row.status)
        await db.refresh(row)
        if row.status == "failed":
            raise Problem(
                502, "EMIAS_UNAVAILABLE", f"РЭМД недоступен (код {exc.code})"
            ) from None
        return 202, delivery_payload(row)
    except Problem as exc:
        row = await _lock_delivery(db, row.id)
        if row.status not in mapping.TERMINAL_STATUSES:
            row.status = "failed"
            row.last_error_code = exc.code
            row.last_error_message = exc.detail
        await audit.record(
            db, clinician_id, "EMIAS_SUBMIT", "session", str(session.id), ip=ip
        )
        await db.commit()
        await db.refresh(row)
        raise
    except Exception:
        row = await _lock_delivery(db, row.id)
        if row.status not in mapping.TERMINAL_STATUSES:
            row.status = "failed"
            row.last_error_code = "EMIAS_UNAVAILABLE"
            row.last_error_message = "Непредвиденная ошибка клиента РЕМД"
        await audit.record(
            db, clinician_id, "EMIAS_SUBMIT", "session", str(session.id), ip=ip
        )
        await db.commit()
        await db.refresh(row)
        raise

    row = await _lock_delivery(db, row.id)
    if row.status not in mapping.TERMINAL_STATUSES:
        row.request_id = request_id
        row.document_sha256 = digest
    await audit.record(db, clinician_id, "EMIAS_SUBMIT", "session", str(session.id), ip=ip)
    await db.commit()
    log.info("REMd accepted protocol %s request %s", protocol.id, request_id)
    await db.refresh(row)
    return 202, delivery_payload(row)

# --- callback ----------------------------------------------------------------


def check_callback_token(token: str | None) -> RemdConfig:
    """Гейт callback'а: включённая интеграция + constant-time сверка секрета."""
    cfg = RemdConfig.from_settings()
    if not cfg.enabled:
        raise Problem(
            503, "EMIAS_DISABLED", "Передача в ЕМИАС выключена (CC_REMD_ENABLED=false)"
        )
    if not cfg.callback_token or not hmac.compare_digest(token or "", cfg.callback_token):
        raise Problem(401, "UNAUTHORIZED", "Неверный callback-токен")
    return cfg


async def callback(db: AsyncSession, payload: dict, token: str | None) -> dict:
    """Приём `registerDocumentResult`. Идемпотентно, без клинической сессии."""
    cfg = check_callback_token(token)
    if payload.get("lpu_uid") != cfg.lpu_uid:
        # Чужой МО не может закрыть нашу доставку, даже зная request_id.
        raise Problem(400, "VALIDATION_ERROR", "lpu_uid не совпадает с настройками")
    if payload.get("message_type") != "registerDocumentResult":
        # Callback-эндпоинт принимает только результат регистрации: иначе
        # getMetadataResult мог бы перевести доставку в registered.
        raise Problem(400, "VALIDATION_ERROR", "Ожидается registerDocumentResult")
    request_id = str(payload.get("response_to_request_id") or "").strip()
    if not request_id:
        raise Problem(400, "VALIDATION_ERROR", "Нет response_to_request_id")

    row = await db.scalar(
        select(EmiasDelivery)
        .where(EmiasDelivery.request_id == request_id)
        .with_for_update()
    )
    if row is None:
        raise Problem(404, "UNKNOWN_REQUEST", "Запрос с таким request_id не найден")
    if row.status in mapping.TERMINAL_STATUSES:
        # At-least-once: повторный callback не меняет терминальное состояние.
        return {"lpu_uid": cfg.lpu_uid, "status": "success"}

    outcome = mapping.callback_outcome(payload)
    row.status = outcome["status"]
    row.external_id = outcome["external_id"]
    row.last_error_code = outcome["error_code"]
    row.last_error_message = outcome["error_message"]
    row.result = outcome["result"]
    if outcome["status"] == "registered":
        row.registered_at = mapping.utcnow()
        protocol = await db.get(SessionProtocol, row.protocol_id)
        if protocol is not None and protocol.status == "signed":
            protocol.status = "submitted"
    await audit.record(
        db, None, "EMIAS_CALLBACK", "session", str(row.session_id), ip=None
    )
    await db.commit()
    log.info("REMd callback request %s → %s", request_id, row.status)
    return {"lpu_uid": cfg.lpu_uid, "status": "success"}


async def get_delivery(db: AsyncSession, clinician_id, session_id) -> dict:
    """Сводка доставки сессии: 404, пока протокол не отправляли."""
    session = await sessions.require_session(db, clinician_id, session_id)
    await sessions.require_patient_access(db, clinician_id, session.patient_id)
    row = await latest_delivery(db, session.id)
    if row is None:
        raise Problem(404, "DELIVERY_NOT_FOUND", "Документ ещё не передавался в ЕМИАС")
    return delivery_payload(row)
