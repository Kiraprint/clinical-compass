"""Очередь эскалаций (открытые и закрытые) и их закрытие (docs/16 §3, docs/04 §3).

Общая очередь для врачей и администратора: сигнал чек-ина не теряется, если у
пациента ещё нет roster-врача. Чтение и закрытие аудируются (152-ФЗ,
спецкатегории ПДн).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CheckinSession, ClinicianRoster, Escalation, User
from app.errors import Problem
from compass import checkin as engine

from .audit import record as audit_record

QUEUE_WINDOW_DAYS = 90
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
# open — очередь работы, resolved — история разбора, all — и то, и другое.
QUEUE_STATUSES = ("open", "resolved", "all")

# В очередь риска попадают только ответы гейта и модуля безопасности:
# остальная симптоматика клиницисту для решения о сигнале не нужна.
RISK_ITEMS = ("HR4", "RS1", "RS2", "RS3")

RESOLUTION_MAX = 1000


def _risk_answers(raw: str | None) -> dict:
    """answers_json — сериализованный JSON из EncryptedText: в очередь идут только HR4/RS."""
    if not raw:
        return {}
    answers = json.loads(raw)
    return {item: answers[item] for item in RISK_ITEMS if item in answers}


def _risk_pattern(raw: str | None) -> str:
    """Паттерн риска для очереди: suicidal | self_harm | negative | unknown.

    Ответы расшифровываются здесь же (EncryptedText), чтобы UI не выводил
    клинический вывод сам: «HR4 = Да при отрицательных RS» — это
    несуицидальное самоповреждение, и его нельзя читать как «всё хорошо».
    """
    if not raw:
        return "unknown"
    try:
        answers = json.loads(raw)
    except (TypeError, ValueError):
        return "unknown"
    return engine.risk_pattern(answers)


def _parse_id(raw: str | uuid.UUID) -> uuid.UUID:
    """Кривой id в пути — это «не найдено», а не ошибка валидации схемы (422)."""
    if isinstance(raw, uuid.UUID):
        return raw
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise Problem(404, "ESCALATION_NOT_FOUND", "Эскалация не найдена") from None


async def list_queue(
    db: AsyncSession,
    actor_id: uuid.UUID,
    ip: str | None = None,
    *,
    status: str = "open",
    limit: int = DEFAULT_LIMIT,
    is_admin: bool = False,
) -> dict:
    """Эскалации за последние 90 дней: `open` (по умолчанию), `resolved`, `all`.

    У `open` предикат и сортировка совпадают с частичным индексом
    `escalations_open_queue`. История закрытых сигналов считает окно по
    `resolved_at` — иначе недавно разобранный старый сигнал из неё выпал бы.
    """
    if status not in QUEUE_STATUSES:
        raise Problem(
            422, "VALIDATION", f"status должен быть одним из: {', '.join(QUEUE_STATUSES)}"
        )
    since = datetime.now(timezone.utc) - timedelta(days=QUEUE_WINDOW_DAYS)
    query = (
        select(
            Escalation,
            User.fio,
            CheckinSession.safety,
            CheckinSession.answers_json,
        )
        .join(User, User.id == Escalation.patient_id)
        .outerjoin(CheckinSession, CheckinSession.id == Escalation.checkin_session_id)
    )
    if status == "open":
        query = query.where(
            Escalation.resolved_at.is_(None),
            Escalation.created_at >= since,
        ).order_by(Escalation.created_at.desc().nulls_last(), Escalation.id.desc())
    elif status == "resolved":
        query = query.where(
            Escalation.resolved_at.is_not(None),
            Escalation.resolved_at >= since,
        ).order_by(Escalation.resolved_at.desc(), Escalation.id.desc())
    else:
        query = query.where(Escalation.created_at >= since).order_by(
            Escalation.created_at.desc().nulls_last(), Escalation.id.desc()
        )
    rows = (await db.execute(query.limit(limit))).all()

    roster: set[uuid.UUID] = set()
    if not is_admin and rows:
        roster = set(
            await db.scalars(
                select(ClinicianRoster.patient_id).where(
                    ClinicianRoster.clinician_id == actor_id,
                    ClinicianRoster.patient_id.in_([e.patient_id for e, *_ in rows]),
                )
            )
        )

    items = [
        {
            "id": str(e.id),
            "patient_id": str(e.patient_id),
            "patient_fio": fio,
            "kind": e.kind,
            "safety": safety,
            "checkin_session_id": str(e.checkin_session_id) if e.checkin_session_id else None,
            "notified_at": e.notified_clinician_at.isoformat() if e.notified_clinician_at else None,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
            "resolution": e.resolution,
            "in_roster": (not is_admin) and e.patient_id in roster,
            "risk_answers": _risk_answers(answers),
            "risk_pattern": _risk_pattern(answers),
        }
        for e, fio, safety, answers in rows
    ]
    await audit_record(db, actor_id, "READ_ESCALATIONS", "escalation", ip=ip)
    await db.commit()
    return {"items": items}


async def resolve(
    db: AsyncSession,
    actor_id: uuid.UUID,
    escalation_id: str | uuid.UUID,
    resolution: str | None = None,
    ip: str | None = None,
) -> dict:
    """Закрыть сигнал: resolved_at + комментарий (шифруется EncryptedText)."""
    escalation_uuid = _parse_id(escalation_id)
    # FOR UPDATE: два врача одновременно не закроют сигнал дважды (409 у второго).
    escalation = await db.scalar(
        select(Escalation).where(Escalation.id == escalation_uuid).with_for_update()
    )
    if escalation is None:
        raise Problem(404, "ESCALATION_NOT_FOUND", "Эскалация не найдена")
    if escalation.resolved_at is not None:
        raise Problem(409, "ESCALATION_ALREADY_RESOLVED", "Эскалация уже закрыта")

    # Длину проверяем здесь, а не в pydantic: иначе клиент получает 422 VALIDATION
    # вместо документированного RESOLUTION_TOO_LONG.
    text = (resolution or "").strip()
    if len(text) > RESOLUTION_MAX:
        raise Problem(
            422, "RESOLUTION_TOO_LONG", f"Комментарий длиннее {RESOLUTION_MAX} символов"
        )

    escalation.resolved_at = datetime.now(timezone.utc)
    escalation.resolution = text or None
    await audit_record(
        db, actor_id, "ESCALATION_RESOLVE", "escalation", object_id=escalation.id, ip=ip
    )
    await db.commit()
    return {
        "id": str(escalation.id),
        "resolved_at": escalation.resolved_at.isoformat(),
        "resolution": escalation.resolution,
    }
