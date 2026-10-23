"""Ежедневный адаптивный чек-ин (docs/16): сеансы, шаги, риск-эскалация.

Детерминированные правила живут в compass.checkin (core); здесь — только
персистентность, валидация API-контракта и побочные эффекты (аудит, уведомления).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import Problem
from app.models import CheckinSession, Escalation
from compass import checkin as engine

from . import push as push_service
from .audit import record as audit_record

MOSCOW = ZoneInfo("Europe/Moscow")

#: Итоговые состояния риска, требующие эскалации (docs/16 §3).
RISK_STATES = ("signal", "review", "unassessed")


def moscow_date() -> "datetime.date":
    return datetime.now(MOSCOW).date()


# --- Сериализация ответа -----------------------------------------------------


def _item_payload(item_id: str) -> dict:
    it = engine.item(item_id)
    return {
        "id": it.id,
        "domain": it.domain,
        "text": it.text,
        "kind": it.kind,
        "anchors": list(it.anchors),
        "emoji": list(it.emoji),
        "max_value": engine.MAX_VALUE[item_id],
    }


def _modules_payload(modules) -> list[dict]:
    return [
        {"code": d.code, "title": d.title, "included": d.included, "reason": d.reason}
        for d in modules
    ]


def _not_started() -> dict:
    return {
        "date": moscow_date().isoformat(),
        "rules_version": engine.RULES_VERSION,
        "status": "not_started",
        "session_id": None,
        "phase": None,
        "step": {"kind": None, "items": []},
        "answers": {},
        "progress": {"answered": 0, "planned": len(engine.CORE_ITEMS)},
        "safety": "none",
        "modules": [],
        "completed_at": None,
    }


def _serialize(session: CheckinSession) -> dict:
    answers = session.answers
    return {
        "date": session.checkin_date.isoformat(),
        "rules_version": session.rules_version,
        "status": session.status,
        "session_id": str(session.id),
        "phase": session.phase,
        "step": {
            "kind": session.phase,
            "items": [_item_payload(i) for i in engine.plan(answers).items],
        },
        "answers": answers,
        "progress": {
            "answered": len(answers),
            "planned": len(engine.planned_items(answers)),
        },
        "safety": session.safety,
        "modules": _modules_payload(engine.plan(answers).modules),
        "completed_at": session.completed_at.isoformat() if session.completed_at else None,
    }


# --- Запросы -----------------------------------------------------------------


def _session_for_day(db: Session, patient_id: uuid.UUID) -> CheckinSession | None:
    return db.scalar(
        select(CheckinSession).where(
            CheckinSession.patient_id == patient_id,
            CheckinSession.checkin_date == moscow_date(),
        )
    )


def today(db: Session, patient_id: uuid.UUID) -> dict:
    session = _session_for_day(db, patient_id)
    return _not_started() if session is None else _serialize(session)


# --- Начало сеанса -----------------------------------------------------------


def start(db: Session, patient_id: uuid.UUID) -> tuple[dict, bool]:
    """Идемпотентный старт: возвращает (тело, создан_ли_сеанс)."""
    existing = _session_for_day(db, patient_id)
    if existing is not None:
        if existing.status == "completed":
            raise Problem(409, "CHECKIN_ALREADY_DONE", "Чек-ин за сегодня уже завершён")
        return _serialize(existing), False

    session = CheckinSession(
        patient_id=patient_id,
        checkin_date=moscow_date(),
        rules_version=engine.RULES_VERSION,
        phase="core",
        status="in_progress",
        safety="none",
    )
    session.answers = {}
    session.presented = {
        "planned": list(engine.CORE_ITEMS),
        "steps": [],
        "modules": [],
    }
    db.add(session)
    try:
        db.flush()
    except IntegrityError:
        # Параллельный старт выиграл гонку — отдаём его сеанс.
        db.rollback()
        existing = _session_for_day(db, patient_id)
        if existing is None:  # pragma: no cover - защита от странного конфликта
            raise
        return _serialize(existing), False
    audit_record(db, patient_id, "CHECKIN_START", "checkin_session", str(session.id))
    db.commit()
    db.refresh(session)
    return _serialize(session), True


# --- Ответы на шаг -----------------------------------------------------------


def _validate_step(payload: dict, allowed: tuple[str, ...]) -> None:
    for item_id, value in payload.items():
        if item_id not in engine.MAX_VALUE or item_id not in allowed:
            raise Problem(
                422, "CHECKIN_UNKNOWN_ITEM", f"Пункт {item_id} не предъявлен на этом шаге"
            )
    for item_id, value in payload.items():
        max_value = engine.MAX_VALUE[item_id]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= max_value:
            raise Problem(
                422,
                "CHECKIN_OUT_OF_RANGE",
                f"Значение для пункта {item_id} должно быть целым в диапазоне 0..{max_value}",
            )
    missing = [i for i in allowed if i not in payload]
    if missing:
        raise Problem(
            422, "CHECKIN_INCOMPLETE", f"Не отвечены пункты шага: {', '.join(missing)}"
        )


def submit(
    db: Session, patient_id: uuid.UUID, session_id: uuid.UUID, payload: dict
) -> dict:
    session = db.scalar(
        select(CheckinSession)
        .where(CheckinSession.id == session_id, CheckinSession.patient_id == patient_id)
        .with_for_update()
    )
    if session is None:
        raise Problem(404, "CHECKIN_NOT_FOUND", "Сеанс чек-ина не найден")
    if session.status == "completed":
        raise Problem(409, "CHECKIN_ALREADY_DONE", "Чек-ин за сегодня уже завершён")

    answers = session.answers
    current = engine.plan(answers)
    if current.phase == "done":  # pragma: no cover - страховка от рассинхрона статуса
        raise Problem(409, "CHECKIN_ALREADY_DONE", "Чек-ин за сегодня уже завершён")

    _validate_step(payload, current.items)

    answers = {**answers, **payload}
    new_plan = engine.plan(answers)
    session.answers = answers

    presented = session.presented
    presented.setdefault("steps", []).append(
        {
            "phase": current.phase,
            "items": list(current.items),
            "modules": _modules_payload(new_plan.modules),
        }
    )
    presented["planned"] = list(engine.planned_items(answers))
    presented["modules"] = _modules_payload(new_plan.modules)
    session.presented = presented
    session.phase = new_plan.phase
    session.safety = new_plan.safety

    escalation = None
    recipients: list[uuid.UUID] = []
    if new_plan.phase == "done":
        session.status = "completed"
        session.completed_at = datetime.now(timezone.utc)
        audit_record(db, patient_id, "CHECKIN_COMPLETE", "checkin_session", str(session.id))
        if new_plan.safety in RISK_STATES:
            escalation = Escalation(
                kind="CHECKIN_RISK",
                patient_id=patient_id,
                checkin_session_id=session.id,
            )
            db.add(escalation)
            db.flush()  # escalation.id для Notification
            recipients = push_service.notify_escalation_clinicians(db, patient_id, escalation)
            audit_record(
                db, patient_id, "CHECKIN_RISK_SIGNAL", "checkin_session", str(session.id)
            )

    db.commit()
    db.refresh(session)

    if escalation is not None and recipients:
        push_service.push_to_clinicians(
            db,
            recipients,
            "Сигнал риска чек-ина",
            "Уведомление врачу: положительный риск-ответ в ежедневном чек-ине",
        )
    return _serialize(session)
