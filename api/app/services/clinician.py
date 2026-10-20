"""Панель врача (FR-8) и витрина пациента: roster-доступ, отчёты, тренды (FR-7)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from compass.models import AUDIENCE_CLINICIAN, AUDIENCE_PATIENT, RiskFlag, ScaleResult
from compass.routing import routing_for
from compass.scales import ALL_SCALES
from compass.trend import trends

from app.errors import Problem
from app.models import AdherenceRecord, AdverseEvent, Answer, Assessment, ChatMessage, ClinicianRoster, Consent, DiaryEntry, Escalation, Prescription, Run, User, Visit

from .audit import record as audit_record
from .history_extract import extract_history


def roster_names(db: Session, clinician_id) -> list[str]:
    """ФИО пациентов врача — deny-list для деперсонализации (docs/12).

    Только непустые fio; пустой список → маскируются лишь структурные сущности.
    """
    rows = db.execute(
        select(User.fio)
        .join(ClinicianRoster, ClinicianRoster.patient_id == User.id)
        .where(ClinicianRoster.clinician_id == clinician_id, User.fio.is_not(None))
    ).all()
    return [r[0] for r in rows if r[0].strip()]


def _require_roster(db: Session, clinician_id, patient_id) -> None:
    row = db.get(ClinicianRoster, (clinician_id, patient_id))
    if row is None:
        raise Problem(403, "NO_ROSTER", "Пациент не закреплён за этим врачом")

def _prescribed_scales_of(db: Session, patient_id) -> list[str]:
    """Коды назначенных пациенту шкал (упорядочены по времени назначения)."""
    rows = db.scalars(
        select(Prescription).where(Prescription.patient_id == patient_id).order_by(Prescription.created_at)
    ).all()
    return [r.scale_code for r in rows]

def prescribe_scales(db: Session, clinician_id, patient_id, scale_codes: list[str], ip: str | None = None) -> dict:
    """Врач назначает пациенту множество шкал скрининга (item 1, multi-scale).

    Заменяет весь набор: отсутствующие в scale_codes удаляются, новые добавляются,
    уже назначенные сохраняют created_at (стабильный порядок). Roster-gate 403.
    Validity кодов — через _scale (404). Минимум одна шкала.
    """
    from app.services.screening import _scale

    # уникальные, непустые, валидные коды
    uniq: list[str] = []
    for code in scale_codes:
        c = code.strip()
        if c and c not in uniq:
            scale = _scale(c)
            if scale.audience != AUDIENCE_PATIENT:
                raise Problem(422, "SCALE_NOT_PATIENT_ADMINISTERED",
                              f"Шкала {c} заполняется только врачом и не назначается пациенту")
            uniq.append(c)
    if not uniq:
        raise Problem(422, "VALIDATION", "Нужна хотя бы одна шкала")
    _require_roster(db, clinician_id, patient_id)
    existing = {r.scale_code: r for r in db.scalars(
        select(Prescription).where(Prescription.patient_id == patient_id)
    ).all()}
    for code in list(existing):
        if code not in uniq:
            db.delete(existing[code])
    for code in uniq:
        if code not in existing:
            db.add(Prescription(patient_id=patient_id, scale_code=code))
    audit_record(db, clinician_id, "PRESCRIBE_SCALE", "user", str(patient_id), ip=ip)
    db.commit()
    return {"patient_id": str(patient_id), "scales": uniq}

def create_clinician_assessment(
    db: Session, clinician_id, patient_id, questionnaire_code: str,
    answers: dict[str, int], ip: str | None = None,
) -> dict:
    """Врач заполняет клиницистскую шкалу (HDRS-17) за пациента.

    Создаёт Run(mode=CLINICIAN) с ответами и переиспользует finish-логику скрининга:
    assessment, триаж и RED-эскалация — тот же контракт ответа. Roster-гейт 403;
    шкала должна быть клиницистской, иначе 422 SCALE_NOT_CLINICIAN_ADMINISTERED.
    """
    from app.services.screening import _scale, _validate_answers, finish_screening

    _require_roster(db, clinician_id, patient_id)
    scale = _scale(questionnaire_code)
    if scale.audience != AUDIENCE_CLINICIAN:
        raise Problem(422, "SCALE_NOT_CLINICIAN_ADMINISTERED",
                      f"Шкала {scale.code} заполняется пациентом, а не врачом")
    run = Run(patient_id=patient_id, questionnaire_code=scale.code, mode="CLINICIAN")
    _validate_answers(db, run, answers)  # до вставки Run — при ошибке ничего не остаётся
    db.add(run)
    db.flush()
    for key, value in answers.items():
        db.add(Answer(run_id=run.id, item_key=key, value=value))
    db.flush()
    return finish_screening(db, run.id, patient_id, audit_action="CLINICIAN_ASSESSMENT", ip=ip)


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


def _patient_card(db: Session, patient_id) -> dict:
    """Сборка карты пациента (только данные; roster/consent/audit — у вызывающих).

    Повторно используется report() и patient_context() ассистента (docs/07 §7).
    """
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

    # Приверженность лечению за 14 дней (задача кейса 7): агрегат без roster-проверки
    # (роster/consent проверяют вызывающие report()/patient_context()).
    since_adh = datetime.now(timezone.utc) - timedelta(days=14)
    adh_rows = db.scalars(
        select(AdherenceRecord)
        .where(AdherenceRecord.patient_id == patient_id, AdherenceRecord.created_at >= since_adh)
        .order_by(AdherenceRecord.created_at.desc())
    ).all()
    adh_taken = sum(1 for r in adh_rows if r.status == "taken")
    adh_partial = sum(1 for r in adh_rows if r.status == "partial")
    adh_total = len(adh_rows)
    adh_pct = round(((adh_taken + 0.5 * adh_partial) / adh_total) * 100, 1) if adh_total else None
    adherence = {
        "pct": adh_pct, "total": adh_total,
        "taken": adh_taken, "partial": adh_partial,
        "missed": adh_total - adh_taken - adh_partial,
        "records": [_adherence_dict(r) for r in adh_rows[:10]],
    }

    # Маршрутизация к психиатру (задача кейса 8, усл.6): детерминированная
    # рекомендация из последнего assessment + тренда. Категории обязат/желательно.
    routing = None
    if last is not None:
        flags = [RiskFlag(kind=f["kind"], item_key=f.get("item_key")) for f in (last.risk_flags or [])]
        r_obj = routing_for(
            last.triage, flags, last.severity,
            timeline["trend"].get("delta_7"),
        )
        routing = {
            "category": r_obj.category, "label": r_obj.label,
            "target": r_obj.target, "reason": r_obj.reason,
        }

    return {
        "patient_id": str(patient_id),
        "email": patient.email,
        "fio": patient.fio,
        "prescribed_scales": _prescribed_scales_of(db, patient_id),
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
        "routing": routing,
        "adherence": adherence,
        "adverse_events": [
            _adverse_event_dict(e) for e in db.scalars(
                select(AdverseEvent)
                .where(AdverseEvent.patient_id == patient_id)
                .order_by(AdverseEvent.onset_date.desc(), AdverseEvent.created_at.desc())
                .limit(20)
            ).all()
        ],
        "diary": [
            {"id": str(e.id), "content": e.content, "mood": e.mood, "sleep_h": e.sleep_h, "created_at": e.created_at.isoformat()}
            for e in diary
        ],
        "available_scales": [
            {
                "code": s.code, "title": s.title, "audience": s.audience,
                "items": [{"key": i.key, "text": i.text, "max_value": i.max_value} for i in s.items],
            }
            for s in ALL_SCALES.values()
        ],
        "open_escalations": [
            {"id": str(e.id), "kind": e.kind, "notified_at": e.notified_clinician_at.isoformat() if e.notified_clinician_at else None}
            for e in escalations
        ],
    }


def report(db: Session, clinician_id, patient_id, ip: str | None = None) -> dict:
    """Полная карта пациента для панели врача (FR-8). Roster + READ_REPORT audit."""
    _require_roster(db, clinician_id, patient_id)
    card = _patient_card(db, patient_id)
    audit_record(db, clinician_id, "READ_REPORT", "patient", str(patient_id), ip=ip)
    db.commit()
    return card


def patient_context(db: Session, clinician_id, patient_id, ip: str | None = None) -> dict:
    """Компактный контекст пациента для ИИ-ассистента врача (docs/07 §7).

    Спецкатегория ПДн (152-ФЗ): roster-gate + активное согласие пациента +
    append-only audit ASK_ASSISTANT_WITH_PATIENT. Транзакцию коммитит вызывающий
    роутер /assistant/ask. В Qdrant НЕ попадает — только grounding ответа.
    """
    _require_roster(db, clinician_id, patient_id)
    active = db.scalar(
        select(Consent).where(Consent.user_id == patient_id, Consent.revoked_at.is_(None))
    )
    if active is None:
        raise Problem(403, "CONSENT_REQUIRED", "Нет активного согласия пациента — карта недоступна (152-ФЗ)")
    card = _patient_card(db, patient_id)
    audit_record(db, clinician_id, "ASK_ASSISTANT_WITH_PATIENT", "patient", str(patient_id), ip=ip)
    return {
        "patient_id": card["patient_id"],
        "fio": card["fio"],
        "prescribed_scales": card["prescribed_scales"],
        "last_assessment": card["last_assessment"],
        "trend": card["trend"],
        "routing": card["routing"],
        "open_escalations": card["open_escalations"],
        "recent_diary": card["diary"][:3],
    }


def create_adverse_event(
    db: Session, clinician_id, patient_id,
    event_text: str, severity: str, onset_date: date,
    suspected_drug: str | None, action_taken: str | None,
    ip: str | None = None,
) -> dict:
    """Регистрация нежелательного явления (задача кейса 3). Roster + аудит ADVERSE_EVENT."""
    _require_roster(db, clinician_id, patient_id)
    ev = AdverseEvent(
        patient_id=patient_id, clinician_id=clinician_id,
        event_text=event_text, severity=severity, onset_date=onset_date,
        suspected_drug=suspected_drug, action_taken=action_taken,
    )
    db.add(ev)
    db.flush()
    audit_record(db, clinician_id, "ADVERSE_EVENT", "patient", str(patient_id), ip=ip)
    db.commit()
    return _adverse_event_dict(ev)


def list_adverse_events(db: Session, clinician_id, patient_id) -> list[dict]:
    """Список НЯ пациента (задача кейса 3). Roster-гейт."""
    _require_roster(db, clinician_id, patient_id)
    rows = db.scalars(
        select(AdverseEvent)
        .where(AdverseEvent.patient_id == patient_id)
        .order_by(AdverseEvent.onset_date.desc(), AdverseEvent.created_at.desc())
    ).all()
    return [_adverse_event_dict(e) for e in rows]


def _adverse_event_dict(e: AdverseEvent) -> dict:
    return {
        "id": str(e.id),
        "event_text": e.event_text,
        "severity": e.severity,
        "onset_date": e.onset_date.isoformat(),
        "suspected_drug": e.suspected_drug,
        "action_taken": e.action_taken,
        "created_at": e.created_at.isoformat(),
    }


def _adherence_dict(r: AdherenceRecord) -> dict:
    return {
        "id": str(r.id),
        "medication": r.medication,
        "status": r.status,
        "note": r.note,
        "created_at": r.created_at.isoformat(),
    }


def create_visit(
    db: Session, clinician_id, patient_id,
    visited_at: datetime, visit_type: str, duration_min: int | None, notes: str,
    ip: str | None = None,
) -> dict:
    """Запись приёма (задача кейса 6, п. «приём»). Roster + аудит VISIT."""
    _require_roster(db, clinician_id, patient_id)
    v = Visit(
        patient_id=patient_id, clinician_id=clinician_id,
        visited_at=visited_at, visit_type=visit_type,
        duration_min=duration_min, notes=notes,
    )
    db.add(v)
    db.flush()
    audit_record(db, clinician_id, "VISIT", "patient", str(patient_id), ip=ip)
    db.commit()
    return _visit_dict(v)


def list_visits(db: Session, clinician_id, patient_id) -> list[dict]:
    """История приёмов пациента (задача кейса 6). Roster-гейт."""
    _require_roster(db, clinician_id, patient_id)
    rows = db.scalars(
        select(Visit)
        .where(Visit.patient_id == patient_id)
        .order_by(Visit.visited_at.desc(), Visit.created_at.desc())
    ).all()
    return [_visit_dict(v) for v in rows]


def _visit_dict(v: Visit) -> dict:
    return {
        "id": str(v.id),
        "visited_at": v.visited_at.isoformat(),
        "visit_type": v.visit_type,
        "duration_min": v.duration_min,
        "notes": v.notes,
        "created_at": v.created_at.isoformat(),
    }

def delete_visit(
    db: Session, clinician_id, patient_id, visit_id,
    ip: str | None = None,
) -> None:
    """Удаление ошибочной записи приёма. Roster + аудит VISIT_DELETE."""
    _require_roster(db, clinician_id, patient_id)
    v = db.get(Visit, visit_id)
    if v is None or v.patient_id != patient_id:
        raise Problem(404, "NOT_FOUND", "Запись приёма не найдена")
    db.delete(v)
    audit_record(db, clinician_id, "VISIT_DELETE", "patient", str(patient_id), ip=ip)
    db.commit()


def create_adherence(
    db: Session, patient_id, medication: str | None, status: str, note: str | None,
) -> dict:
    """Пациент отмечает приверженность лечению (задача кейса 7). consent-гейт у роутера."""
    rec = AdherenceRecord(patient_id=patient_id, medication=medication, status=status, note=note)
    db.add(rec)
    db.commit()
    return _adherence_dict(rec)


MOSCOW = ZoneInfo("Europe/Moscow")


def my_adherence(db: Session, patient_id, days: int = 14) -> dict:
    """Пациент читает свои отметки приверженности (экран «Сегодня»). Consent-гейт у роутера.

    Возвращает записи за окно (свежие первыми) и отметку за текущие сутки по Москве,
    чтобы экран показывал «сегодня уже отмечено».
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.scalars(
        select(AdherenceRecord)
        .where(AdherenceRecord.patient_id == patient_id, AdherenceRecord.created_at >= since)
        .order_by(AdherenceRecord.created_at.desc())
    ).all()
    today = datetime.now(MOSCOW).date()
    today_rec = next(
        (r for r in rows if r.created_at.astimezone(MOSCOW).date() == today), None
    )
    return {
        "items": [_adherence_dict(r) for r in rows],
        "today": _adherence_dict(today_rec) if today_rec is not None else None,
    }


def adherence_summary(db: Session, clinician_id, patient_id, days: int = 14) -> dict:
    """Агрегат приверженности для карточки врача (задача кейса 7). Roster-гейт.

    % приверженности = (taken*1 + partial*0.5) / всего записей за окно.
    """
    _require_roster(db, clinician_id, patient_id)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.scalars(
        select(AdherenceRecord)
        .where(AdherenceRecord.patient_id == patient_id, AdherenceRecord.created_at >= since)
        .order_by(AdherenceRecord.created_at.desc())
    ).all()
    taken = sum(1 for r in rows if r.status == "taken")
    partial = sum(1 for r in rows if r.status == "partial")
    missed = sum(1 for r in rows if r.status == "missed")
    total = len(rows)
    pct = round(((taken + 0.5 * partial) / total) * 100, 1) if total else None
    return {
        "days": days,
        "total": total,
        "taken": taken, "partial": partial, "missed": missed,
        "pct": pct,
        "records": [_adherence_dict(r) for r in rows[:20]],
    }


def _chat_dict(m: ChatMessage) -> dict:
    return {
        "id": str(m.id),
        "sender": m.sender,
        "kind": m.kind,
        "content": m.content,
        "created_at": m.created_at.isoformat(),
    }


def clinician_post_chat(db: Session, clinician_id, patient_id, content: str, ip: str | None = None) -> dict:
    """Врач пишет пациенту в канал коммуникации (задача кейса 6). Roster + аудит CLINICIAN_CHAT."""
    _require_roster(db, clinician_id, patient_id)
    msg = ChatMessage(patient_id=patient_id, sender="CLINICIAN", kind="TEXT", content=content)
    db.add(msg)
    db.flush()
    audit_record(db, clinician_id, "CLINICIAN_CHAT", "patient", str(patient_id), ip=ip)
    db.commit()
    return _chat_dict(msg)


def clinician_list_chat(db: Session, clinician_id, patient_id) -> list[dict]:
    """История переписки с пациентом (задача кейса 6). Roster-гейт."""
    _require_roster(db, clinician_id, patient_id)
    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.patient_id == patient_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(50)
    ).all()
    # вернуть в хронологическом порядке (последние 50)
    return [_chat_dict(m) for m in reversed(rows)]


def import_history(db: Session, clinician_id, patient_id, text: str, template_name: str | None, ip: str | None = None) -> dict:
    """Разбор клинической истории (метрика кейса 2). Roster + аудит IMPORT_HISTORY.

    Возвращает ЧЕРНОВИК извлечённых полей — врач подтверждает вручную. В клиническую
    модель не пишется (структурирование, не клиническое действие).
    """
    _require_roster(db, clinician_id, patient_id)
    ext = extract_history(text, template_name)
    audit_record(db, clinician_id, "IMPORT_HISTORY", "patient", str(patient_id), ip=ip)
    db.commit()
    return {
        "template": ext.template,
        "fields_found": ext.fields_found,
        "complaints": ext.complaints,
        "anamnesis": ext.anamnesis,
        "diagnosis_codes": list(ext.diagnosis_codes),
        "therapy": list(ext.therapy),
        "scale_scores": list(ext.scale_scores),
        "requires_confirmation": True,
    }