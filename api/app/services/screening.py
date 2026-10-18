"""Сервис: скрининг, чек-ины, эскалации. Домен — движок core (чистые функции).

Инварианты (docs/04 §6): assessment считает сервер; эскалация RED — в той же
транзакции finish; чек-ин не чаще 1 раза в сутки (по времени Москвы).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from compass.checkin import generate_checkin
from compass.models import ScaleResult
from compass.scales import ALL_SCALES, PHQ9
from compass.scoring import score, risk_flags
from compass.triage import triage

from app.errors import Problem
from app.models import Answer, Assessment, Escalation, Prescription, Run, User

from .audit import record as audit_record
from . import push as push_service

MOSCOW = ZoneInfo("Europe/Moscow")

# Контакты помощи — показываются пациенту при эскалации RED
HELP_RESOURCES = [
    {"title": "Единый телефон доверия", "phone": "8-800-2000-122"},
    {"title": "Экстренная служба", "phone": "112"},
]


def _scale(code: str):
    scale = ALL_SCALES.get(code)
    if scale is None:
        raise Problem(404, "NOT_FOUND", f"Неизвестная шкала: {code}")
    return scale


def _moscow_today() -> tuple[datetime, datetime]:
    now = datetime.now(MOSCOW)
    start = datetime(now.year, now.month, now.day, tzinfo=MOSCOW)
    return start, start + timedelta(days=1)


def _own_run(db: Session, run_id, patient_id) -> Run:
    run = db.get(Run, run_id)
    if run is None or run.patient_id != patient_id:
        raise Problem(404, "NOT_FOUND", "Прогон не найден")
    return run


# Шкала первой линии при отсутствии предписания врача (item 1: пациент не выбирает шкалу).
DEFAULT_SCALE = PHQ9.code

def prescribed_scales(db: Session, patient_id) -> list[str]:
    """Шкалы, назначенные пациенту врачом (упорядочены по времени назначения).
    Пусто → дефолт первой линии [PHQ-9] (пациент без назначения всё равно может скриниться)."""
    rows = db.scalars(
        select(Prescription).where(Prescription.patient_id == patient_id).order_by(Prescription.created_at)
    ).all()
    codes = [r.scale_code for r in rows]
    if not codes:
        return [DEFAULT_SCALE]
    return codes

def prescribed_scale(db: Session, patient_id) -> str:
    """Первичная (первая назначенная) шкала — для чек-инов и дефолта старта. Без назначения — PHQ-9."""
    return prescribed_scales(db, patient_id)[0]

def start_screening(db: Session, patient_id, questionnaire_code: str | None = None) -> Run:
    """Старт скрининга. Шкала: явно указанная пациентом (если она в назначенном множестве),
    иначе первичная. Пациент не может выбрать произвольную шкалу — 403 SCALE_NOT_PRESCRIBED."""
    assigned = prescribed_scales(db, patient_id)
    if questionnaire_code is None:
        code = assigned[0]
    elif questionnaire_code in assigned:
        code = questionnaire_code
    else:
        raise Problem(403, "SCALE_NOT_PRESCRIBED",
                      f"Шкала {questionnaire_code} не назначена врачом")
    scale = _scale(code)
    open_run = db.scalar(
        select(Run).where(Run.patient_id == patient_id, Run.mode == "SCREENING", Run.finished_at.is_(None))
    )
    if open_run is not None:
        raise Problem(409, "CONFLICT", "Уже есть незавершённый скрининг")
    run = Run(patient_id=patient_id, questionnaire_code=scale.code, mode="SCREENING")
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _validate_answers(db: Session, run: Run, answers: Mapping[str, int]) -> None:
    scale = _scale(run.questionnaire_code)
    for key, value in answers.items():
        if key not in scale.item_keys():
            raise Problem(422, "VALIDATION", f"Пункт {key} отсутствует в шкале {scale.code}")
        if not isinstance(value, int) or value < 0 or value > scale.item(key).max_value:
            raise Problem(422, "VALIDATION", f"Значение {value} вне диапазона для пункта {key}")


def add_answers(db: Session, run_id, patient_id, answers: Mapping[str, int]) -> None:
    run = _own_run(db, run_id, patient_id)
    if run.finished_at is not None:
        raise Problem(409, "RUN_CLOSED", "Прогон уже завершён")
    _validate_answers(db, run, answers)
    existing = set(db.scalars(select(Answer.item_key).where(Answer.run_id == run_id)))
    overlap = set(answers) & existing
    if overlap:
        db.execute(delete(Answer).where(Answer.run_id == run_id, Answer.item_key.in_(overlap)))
    for key, value in answers.items():
        db.add(Answer(run_id=run_id, item_key=key, value=value))
    db.commit()


def finish_screening(db: Session, run_id, patient_id) -> dict:
    run = _own_run(db, run_id, patient_id)
    if run.finished_at is not None:
        raise Problem(409, "RUN_CLOSED", "Прогон уже завершён")
    scale = _scale(run.questionnaire_code)
    rows = db.scalars(select(Answer).where(Answer.run_id == run_id)).all()
    answers = {a.item_key: a.value for a in rows}
    try:
        result = score(scale, answers)
    except ValueError as exc:
        raise Problem(422, "VALIDATION", str(exc))
    flags = risk_flags(scale, answers)
    level = triage(result, flags)

    assessment = Assessment(
        run_id=run_id,
        patient_id=patient_id,
        total_score=result.total_score,
        severity=result.severity,
        triage=level,
        risk_flags=[{"kind": f.kind, "item_key": f.item_key} for f in flags],
    )
    db.add(assessment)
    db.flush()  # назначает assessment.id до создания escalation
    run.finished_at = datetime.now(timezone.utc)

    escalation = None
    recipient_ids: list = []
    if level == "RED":
        kind = "SUICIDAL" if any(f.kind == "SUICIDAL_IDEATION" for f in flags) else "SEVERE"
        escalation = Escalation(assessment_id=assessment.id, patient_id=patient_id, kind=kind)
        db.add(escalation)
        db.flush()  # escalation.id до создания Notification
        recipient_ids = push_service.notify_escalation_clinicians(db, patient_id, escalation)
    audit_record(db, patient_id, "SCREENING_FINISH", "assessment", str(run_id))
    db.commit()
    db.refresh(assessment)

    # Web Push — после коммита (сбои не пробрасываются).
    if escalation is not None and recipient_ids:
        push_service.push_to_clinicians(
            db,
            recipient_ids,
            "RED-эскалация",
            "Уведомление врачу: у пациента RED-триаж, см. карточку пациента",
        )

    resp: dict = {
        "assessment_id": str(assessment.id),
        "questionnaire_code": scale.code,
        "total_score": assessment.total_score,
        "severity": assessment.severity,
        "triage": assessment.triage,
        "risk_flags": assessment.risk_flags,
        "computed_at": assessment.computed_at.isoformat(),
    }
    if escalation is not None:
        db.refresh(escalation)
        resp["escalation"] = {
            "id": str(escalation.id),
            "kind": escalation.kind,
            "help_resources": HELP_RESOURCES,
        }
    return resp

def abandon_open(db: Session, patient_id) -> dict:
    """Закрывает открытый (незавершённый) скрининг пациента без assessment.

    Нужно пациенту, который бросил скрининг на полпути: иначе повторный
    /screenings вернёт 409 CONFLICT (runs_screening_open) навсегда.
    """
    run = db.scalar(
        select(Run).where(Run.patient_id == patient_id, Run.mode == "SCREENING", Run.finished_at.is_(None))
    )
    if run is None:
        return {"abandoned": False}
    run.finished_at = datetime.now(timezone.utc)
    audit_record(db, patient_id, "SCREENING_ABANDON", "run", str(run.id))
    db.commit()
    return {"abandoned": True, "run_id": str(run.id)}


def _last_scale_result(db: Session, patient_id) -> tuple:
    """Последние assessment+run пациента для генерации чек-ина (None, None если нет)."""
    row = db.execute(
        select(Assessment, Run)
        .join(Run, Run.id == Assessment.run_id)
        .where(Assessment.patient_id == patient_id)
        .order_by(Assessment.computed_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None, None
    assessment, run = row
    rows = db.scalars(select(Answer).where(Answer.run_id == run.id)).all()
    per_item = {a.item_key: a.value for a in rows}
    result = ScaleResult(
        code=run.questionnaire_code,
        total_score=assessment.total_score,
        severity=assessment.severity,
        per_item=per_item,
    )
    return result, run.questionnaire_code


def today_checkin(db: Session, patient_id) -> dict:
    scale = _scale(_last_scale_result(db, patient_id)[1] or prescribed_scale(db, patient_id))
    last_result, _ = _last_scale_result(db, patient_id)
    checkin = generate_checkin(scale, last_result)
    items = checkin.items
    start, end = _moscow_today()
    done = db.scalar(
        select(Run).where(
            Run.patient_id == patient_id, Run.mode == "CHECKIN", Run.started_at >= start, Run.started_at < end
        )
    )
    return {
        "items": [{"key": i.key, "text": i.text, "max_value": i.max_value} for i in items],
        "done": done is not None,
    }


def complete_checkin(db: Session, patient_id, answers: Mapping[str, int]) -> dict:
    start, end = _moscow_today()
    existing = db.scalar(
        select(Run).where(
            Run.patient_id == patient_id, Run.mode == "CHECKIN", Run.started_at >= start, Run.started_at < end
        )
    )
    if existing is not None:
        raise Problem(409, "RUN_ALREADY_TODAY", "Чек-ин за сегодня уже выполнен")
    scale = _scale(_last_scale_result(db, patient_id)[1] or prescribed_scale(db, patient_id))
    last_result, _ = _last_scale_result(db, patient_id)
    checkin = generate_checkin(scale, last_result)
    items = checkin.items
    item_keys = {i.key for i in items}
    max_by_key = {i.key: i.max_value for i in items}
    for key in answers:
        if key not in item_keys:
            raise Problem(422, "VALIDATION", f"Пункт {key} не входит в сегодняшний чек-ин")
        value = answers[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > max_by_key[key]:
            raise Problem(422, "VALIDATION", f"Значение {value!r} для пункта {key} вне диапазона 0..{max_by_key[key]}")
    run = Run(patient_id=patient_id, questionnaire_code=scale.code, mode="CHECKIN", finished_at=datetime.now(timezone.utc))
    db.add(run)
    db.flush()
    for key, value in answers.items():
        db.add(Answer(run_id=run.id, item_key=key, value=value))
    audit_record(db, patient_id, "CHECKIN_COMPLETE", "run", str(run.id))
    db.commit()
    return {"run_id": str(run.id), "items": [i.key for i in items], "answered": sorted(answers)}