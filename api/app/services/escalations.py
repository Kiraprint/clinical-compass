"""Очередь открытых эскалаций и их закрытие (docs/16 §3, docs/04 §3).

Общая очередь для врачей и администратора: сигнал чек-ина не теряется, если у
пациента ещё нет roster-врача. Чтение и закрытие аудируются (152-ФЗ,
спецкатегории ПДн).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CheckinSession, ClinicianRoster, Escalation, User
from app.errors import Problem

from .audit import record as audit_record

QUEUE_WINDOW_DAYS = 90
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

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

def list_open(
    db: Session,
    actor_id: uuid.UUID,
    ip: str | None = None,
    *,
    limit: int = DEFAULT_LIMIT,
    is_admin: bool = False,
) -> dict:
    """Открытые эскалации за последние 90 дней, новые сверху."""
    since = datetime.now(timezone.utc) - timedelta(days=QUEUE_WINDOW_DAYS)
    rows = db.execute(
        select(
            Escalation,
            User.fio,
            CheckinSession.safety,
            CheckinSession.answers_json,
        )
        .join(User, User.id == Escalation.patient_id)
        .outerjoin(CheckinSession, CheckinSession.id == Escalation.checkin_session_id)
        .where(
            Escalation.resolved_at.is_(None),
            Escalation.created_at >= since,
        )
        .order_by(Escalation.created_at.desc().nulls_last(), Escalation.id.desc())
        .limit(limit)
    ).all()

    roster: set[uuid.UUID] = set()
    if not is_admin and rows:
        roster = set(
            db.scalars(
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
            "in_roster": (not is_admin) and e.patient_id in roster,
            "risk_answers": _risk_answers(answers),
        }
        for e, fio, safety, answers in rows
    ]
    audit_record(db, actor_id, "READ_ESCALATIONS", "escalation", ip=ip)
    db.commit()
    return {"items": items}


def resolve(
    db: Session,
    actor_id: uuid.UUID,
    escalation_id: uuid.UUID,
    resolution: str | None = None,
    ip: str | None = None,
) -> dict:
    """Закрыть сигнал: resolved_at + комментарий (шифруется EncryptedText)."""
    # FOR UPDATE: два врача одновременно не закроют сигнал дважды (409 у второго).
    escalation = db.scalar(
        select(Escalation).where(Escalation.id == escalation_id).with_for_update()
    )
    if escalation is None:
        raise Problem(404, "ESCALATION_NOT_FOUND", "Эскалация не найдена")
    if escalation.resolved_at is not None:
        raise Problem(409, "ESCALATION_ALREADY_RESOLVED", "Эскалация уже закрыта")

    text = (resolution or "").strip()
    if len(text) > RESOLUTION_MAX:
        raise Problem(
            422, "RESOLUTION_TOO_LONG", f"Комментарий длиннее {RESOLUTION_MAX} символов"
        )

    escalation.resolved_at = datetime.now(timezone.utc)
    escalation.resolution = text or None
    audit_record(
        db, actor_id, "ESCALATION_RESOLVE", "escalation", object_id=escalation.id, ip=ip
    )
    db.commit()
    return {
        "id": str(escalation.id),
        "resolved_at": escalation.resolved_at.isoformat(),
        "resolution": escalation.resolution,
    }
