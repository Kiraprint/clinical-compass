"""Сервис: скрининг, чек-ины, эскалации. Домен — движок core (чистые функции).

Инварианты (docs/04 §6): assessment считает сервер; эскалация RED — в той же
транзакции finish; чек-ин не чаще 1 раза в сутки (по времени Москвы).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from compass.models import AUDIENCE_PATIENT
from compass.scales import ALL_SCALES, PHQ9
from compass.scoring import score, risk_flags
from compass.triage import triage

from app.errors import Problem
from app.models import Answer, Assessment, Escalation, Prescription, Run, User

from .audit import record as audit_record
from . import push as push_service

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
    иначе первичная. Пациент не может выбрать произвольную шкалу — 403 SCALE_NOT_PRESCRIBED.
    Клиницистские шкалы (HDRS-17) пациент не заполняет — 403 SCALE_NOT_PATIENT_ADMINISTERED."""
    if questionnaire_code is not None:
        requested = _scale(questionnaire_code)
        if requested.audience != AUDIENCE_PATIENT:
            raise Problem(403, "SCALE_NOT_PATIENT_ADMINISTERED",
                          f"Шкала {questionnaire_code} заполняется только врачом")
    assigned = prescribed_scales(db, patient_id)
    if questionnaire_code is None:
        code = assigned[0]
    elif questionnaire_code in assigned:
        code = questionnaire_code
    else:
        raise Problem(403, "SCALE_NOT_PRESCRIBED",
                      f"Шкала {questionnaire_code} не назначена врачом")
    scale = _scale(code)
    if scale.audience != AUDIENCE_PATIENT:
        # Защита от легаси-назначений клиницистской шкалы.
        raise Problem(403, "SCALE_NOT_PATIENT_ADMINISTERED",
                      f"Шкала {code} заполняется только врачом")
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


def finish_screening(db: Session, run_id, patient_id, *, audit_action: str = "SCREENING_FINISH",
                     ip: str | None = None) -> dict:
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
    audit_record(db, patient_id, audit_action, "assessment", str(run_id), ip=ip)
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



