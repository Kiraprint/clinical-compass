"""Панель врача (FR-8) и витрина пациента: roster-доступ, отчёты, тренды (FR-7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from compass.models import ScaleResult
from compass.scales import ALL_SCALES
from compass.trend import trends

from app.errors import Problem
from app.models import Assessment, ClinicianRoster, DiaryEntry, Escalation, User

from .audit import record as audit_record


def _require_roster(db: Session, clinician_id, patient_id) -> None:
    row = db.get(ClinicianRoster, (clinician_id, patient_id))
    if row is None:
        raise Problem(403, "NO_ROSTER", "Пациент не закреплён за этим врачом")

def prescribe_scale(db: Session, clinician_id, patient_id, scale_code: str) -> dict:
    """Врач назначает пациенту шкалу скрининга (item 1). Validity через _scale (404)."""
    from app.services.screening import _scale

    _scale(scale_code)  # 404, если неизвестная шкала
    _require_roster(db, clinician_id, patient_id)
    patient = db.get(User, patient_id)
    patient.prescribed_scale = scale_code
    audit_record(db, clinician_id, "PRESCRIBE_SCALE", "user", str(patient_id))
    db.commit()
    return {"patient_id": str(patient_id), "questionnaire_code": scale_code}


def list_patients(db: Session, clinician_id, triage: str | None = None) -> list[dict]:
    rows = db.scalars(select(ClinicianRoster).where(ClinicianRoster.clinician_id == clinician_id)).all()
    cards = []
    for row in rows:
        patient = db.get(User, row.patient_id)
        if patient is None:
            continue
        last = db.scalar(
            select(Assessment)
            .where(Assessment.patient_id == row.patient_id)
            .order_by(Assessment.computed_at.desc())
        )

        last_triage = last.triage if last else None
        if triage is not None and last_triage != triage:
            continue  # игнорируем, если фильтр не совпадает

        open_esc = db.scalar(
            select(func.count())
            .select_from(Escalation)
            .where(Escalation.patient_id == row.patient_id, Escalation.resolved_at.is_(None))
        )
        cards.append({
            "patient_id": str(row.patient_id),
            "email": patient.email,
            "fio": patient.fio,
            "last_triage": last_triage,
            "last_score": last.total_score if last else None,
            "last_severity": last.severity if last else None,
            "last_assessment_at": last.computed_at.isoformat() if last else None,
            "open_escalations": open_esc or 0,
        })
    return cards


def patient_timeline(db: Session, patient_id, days: int) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.scalars(
        select(Assessment)
        .where(Assessment.patient_id == patient_id, Assessment.computed_at >= since)
        .order_by(Assessment.computed_at.asc())
    ).all()
    runs = [
        {
            "assessment_id": str(a.id),
            "total_score": a.total_score,
            "severity": a.severity,
            "triage": a.triage,
            "computed_at": a.computed_at.isoformat(),
        }
        for a in rows
    ]
    results = [
        ScaleResult(code="", total_score=a.total_score, severity=a.severity, per_item={}) for a in rows
    ]
    t = trends(results)
    return {
        "runs": runs,
        "trend": {
            "moving_avg": list(t.moving_avg),
            "delta_7": t.delta_7,
            "delta_30": t.delta_30,
            "slope": t.slope,
        },
    }


def report(db: Session, clinician_id, patient_id) -> dict:
    _require_roster(db, clinician_id, patient_id)
    patient = db.get(User, patient_id)
    if patient is None or patient.role != "PATIENT":
        raise Problem(404, "NOT_FOUND", "Пациент не найден")

    last = db.scalar(
        select(Assessment).where(Assessment.patient_id == patient_id).order_by(Assessment.computed_at.desc())
    )
    history = db.scalars(
        select(Assessment)
        .where(Assessment.patient_id == patient_id)
        .order_by(Assessment.computed_at.desc())
        .limit(10)
    ).all()
    diary = db.scalars(
        select(DiaryEntry).where(DiaryEntry.patient_id == patient_id).order_by(DiaryEntry.created_at.desc()).limit(20)
    ).all()
    escalations = db.scalars(
        select(Escalation)
        .where(Escalation.patient_id == patient_id, Escalation.resolved_at.is_(None))
        .order_by(Escalation.notified_clinician_at.desc().nulls_last())
    ).all()
    timeline = patient_timeline(db, patient_id, 90)

    audit_record(db, clinician_id, "READ_REPORT", "patient", str(patient_id))
    db.commit()

    return {
        "patient_id": str(patient_id),
        "email": patient.email,
        "fio": patient.fio,
        "prescribed_scale": patient.prescribed_scale,
        "last_assessment": (
            {
                "assessment_id": str(last.id),
                "total_score": last.total_score,
                "severity": last.severity,
                "triage": last.triage,
                "computed_at": last.computed_at.isoformat(),
            }
            if last
            else None
        ),
        "history": [
            {"total_score": a.total_score, "severity": a.severity, "triage": a.triage, "computed_at": a.computed_at.isoformat()}
            for a in history
        ],
        "trend": timeline["trend"],
        "diary": [
            {"id": str(e.id), "content": e.content, "mood": e.mood, "sleep_h": e.sleep_h, "created_at": e.created_at.isoformat()}
            for e in diary
        ],
        "available_scales": [
            {"code": s.code, "title": s.title} for s in ALL_SCALES.values()
        ],
        "open_escalations": [
            {"id": str(e.id), "kind": e.kind, "notified_at": e.notified_clinician_at.isoformat() if e.notified_clinician_at else None}
            for e in escalations
        ],
    }