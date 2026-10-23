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
from app.models import AdverseEvent, Answer, Assessment, ChatMessage, CheckinSession, ClinicianRoster, Consent, DiaryEntry, Escalation, Prescription, Run, User, Visit

from . import adherence as adherence_service
from . import diary as diary_service
from .audit import record as audit_record
from .contacts import contacts_payload
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


def require_roster(db: Session, clinician_id, patient_id) -> None:
    """Публичный roster-гейт для других сервисов (FR-22 контакты)."""
    _require_roster(db, clinician_id, patient_id)


def require_patient_consent(db: Session, patient_id) -> None:
    """Публичный consent-гейт для клиницистских разделов карты (FR-23 назначения,
    история пациента). Без активного согласия спецкатегория ПДн недоступна."""
    active = db.scalar(
        select(Consent.id).where(Consent.user_id == patient_id, Consent.revoked_at.is_(None))
    )
    if active is None:
        raise Problem(403, "CONSENT_REQUIRED", "Нет активного согласия пациента — данные недоступны (152-ФЗ)")


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


# FR-30: порядок карточек — красные всегда сверху. Внутри ранга: больше
# открытых эскалаций, затем более свежая оценка, затем ФИО/email (стабильно).
TRIAGE_RANK = {"RED": 0, "YELLOW": 1, "GREEN": 2}


def list_patients(db: Session, clinician_id, triage: str | None = None) -> list[dict]:
    rows = db.scalars(select(ClinicianRoster).where(ClinicianRoster.clinician_id == clinician_id)).all()
    entries: list[tuple[tuple, dict]] = []
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
        contacts = contacts_payload(patient, masked=True)  # FR-22: телефон — только маска
        card = {
            "patient_id": str(row.patient_id),
            "email": patient.email,
            "fio": patient.fio,
            "last_triage": last_triage,
            "last_score": last.total_score if last else None,
            "last_severity": last.severity if last else None,
            "last_assessment_at": last.computed_at.isoformat() if last else None,
            "open_escalations": open_esc or 0,
            "phone": contacts["phone"],
            "birth_date": contacts["birth_date"],
            "age": contacts["age"],
        }
        entries.append((
            (
                TRIAGE_RANK.get(last_triage, 3),
                -(open_esc or 0),
                -last.computed_at.timestamp() if last else float("inf"),
                (patient.fio or patient.email or "").lower(),
            ),
            card,
        ))
    entries.sort(key=lambda e: e[0])
    return [card for _, card in entries]


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
    checkins = db.scalars(
        select(CheckinSession)
        .where(CheckinSession.patient_id == patient_id)
        .order_by(CheckinSession.checkin_date.desc())
        .limit(7)
    ).all()

    # Приверженность за 14 дней (FR-27): weighted-процент от ожидаемых приёмов по
    # режиму назначения. Roster/consent проверяют вызывающие report()/patient_context().
    adherence = adherence_service.summary_payload(adherence_service.compute(db, patient_id, 14))

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

    # FR-22: контакты отдаются только при активном согласии пациента (152-ФЗ);
    # roster-гейт проверяют вызывающие (report/patient_context).
    has_consent = db.scalar(
        select(Consent.id).where(Consent.user_id == patient_id, Consent.revoked_at.is_(None))
    ) is not None
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
            _adverse_event_dict(db, e) for e in db.scalars(
                select(AdverseEvent)
                .where(AdverseEvent.patient_id == patient_id)
                .order_by(AdverseEvent.onset_date.desc(), AdverseEvent.created_at.desc())
                .limit(20)
            ).all()
        ],
        "diary": [
            {
                "id": str(e.id), "content": e.content, "mood": e.mood,
                "mood_scale": diary_service.mood_scale_of(e),
                "sleep_h": e.sleep_h, "created_at": e.created_at.isoformat(),
            }
            for e in diary
        ],
        "available_scales": [
            {
                "code": s.code, "title": s.title, "audience": s.audience,
                "items": [
                    {"key": i.key, "text": i.text, "max_value": i.max_value, "anchors": list(i.anchors)}
                    for i in s.items
                ],
            }
            for s in ALL_SCALES.values()
        ],
        "open_escalations": [
            {
                "id": str(e.id),
                "kind": e.kind,
                "notified_at": e.notified_clinician_at.isoformat() if e.notified_clinician_at else None,
                "checkin_session_id": str(e.checkin_session_id) if e.checkin_session_id else None,
            }
            for e in escalations
        ],
        # docs/16 §3: что видит клиницист — ответы, состояние риска и основания модулей.
        "checkin": {
            "rules_version": checkins[0].rules_version if checkins else None,
            "recent": [_checkin_dict(c) for c in checkins],
        },
        # FR-31: только принятые врачом факты; отзыв согласия удаляет строки.
        "chat_insights": _accepted_chat_insights(db, patient_id) if has_consent else [],
        "contacts": contacts_payload(patient, masked=True) if has_consent else None,
    }


def _accepted_chat_insights(db: Session, patient_id) -> list[dict]:
    """Ленивый импорт: chat_insights зависит от roster/consent-гейтов этого модуля."""
    from . import chat_insights as chat_insights_service

    return chat_insights_service.accepted_for_report(db, patient_id)


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
    """Регистрация НЯ врачом (FR-28 legacy): сразу confirmed, автор — врач."""
    _require_roster(db, clinician_id, patient_id)
    ev = AdverseEvent(
        patient_id=patient_id, clinician_id=clinician_id,
        event_text=event_text, severity=severity, onset_date=onset_date,
        suspected_drug=suspected_drug, action_taken=action_taken,
        reported_by="clinician", status="confirmed",
    )
    db.add(ev)
    db.flush()
    audit_record(db, clinician_id, "ADVERSE_EVENT", "patient", str(patient_id), ip=ip)
    db.commit()
    return _adverse_event_dict(db, ev)


def list_adverse_events(db: Session, clinician_id, patient_id) -> list[dict]:
    """Список НЯ пациента (FR-28): статус проверки + ссылка на справочник."""
    _require_roster(db, clinician_id, patient_id)
    rows = db.scalars(
        select(AdverseEvent)
        .where(AdverseEvent.patient_id == patient_id)
        .order_by(AdverseEvent.onset_date.desc(), AdverseEvent.created_at.desc())
    ).all()
    return [_adverse_event_dict(db, e) for e in rows]


def _adverse_event_dict(db: Session, e: AdverseEvent) -> dict:
    """Единый формат НЯ для врача (см. services/adverse.event_dict)."""
    from .adverse import event_dict

    return event_dict(db, e)




def _checkin_dict(c: CheckinSession) -> dict:
    """Сеанс чек-ина для врача: ответы, риск, включённые модули и их основания."""
    presented = c.presented or {}
    return {
        "session_id": str(c.id),
        "date": c.checkin_date.isoformat(),
        "status": c.status,
        "phase": c.phase,
        "safety": c.safety,
        "rules_version": c.rules_version,
        "answers": c.answers or {},
        "modules": [
            {"code": m.get("code"), "included": m.get("included"), "reason": m.get("reason")}
            for m in presented.get("modules", [])
        ],
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
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
    db: Session, patient_id, medication_id, medication: str | None, status: str, note: str | None,
) -> dict:
    """Пациент отмечает приверженность лечению (FR-27). Consent-гейт у роутера."""
    return adherence_service.create_record(
        db, patient_id, medication_id=medication_id, medication=medication,
        status=status, note=note,
    )


MOSCOW = ZoneInfo("Europe/Moscow")


def my_adherence(db: Session, patient_id, days: int = 14) -> dict:
    """Приверженность пациента (экран «Сегодня» и вкладка). Consent-гейт у роутера.

    Отдаёт отметки за окно, отметку за текущие сутки по Москве, активные
    назначения для выбора и weighted-процент от ожидаемых приёмов (FR-27).
    """
    return adherence_service.compute(db, patient_id, days)


def adherence_summary(db: Session, clinician_id, patient_id, days: int = 14) -> dict:
    """Агрегат приверженности для карточки врача (FR-27). Roster-гейт."""
    _require_roster(db, clinician_id, patient_id)
    return adherence_service.summary_payload(adherence_service.compute(db, patient_id, days))


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


# FR-29: превью последнего сообщения в списке переписок — обрезка, не лог.
CHAT_PREVIEW_MAX = 120


def list_patient_chats(db: Session, clinician_id) -> list[dict]:
    """Переписки врача с roster-пациентами (FR-29): последнее сообщение и счётчик.

    Превью дешифруется сервером (content — EncryptedText) и обрезается до
    CHAT_PREVIEW_MAX; в логи не попадает. Аудит не пишем: сводка для списка —
    чтение внутри уже разрешённой roster-переписки.
    """
    patient_ids = list(db.scalars(
        select(ClinicianRoster.patient_id).where(ClinicianRoster.clinician_id == clinician_id)
    ))
    if not patient_ids:
        return []

    stats = {
        pid: (int(cnt), last_at)
        for pid, cnt, last_at in db.execute(
            select(ChatMessage.patient_id, func.count(), func.max(ChatMessage.created_at))
            .where(ChatMessage.patient_id.in_(patient_ids))
            .group_by(ChatMessage.patient_id)
        )
    }
    if not stats:
        return []

    # Последнее сообщение каждого пациента: оконная функция выбирает id, сами
    # сообщения грузим ORM-сущностью — иначе TypeDecorator не расшифрует content.
    rn = func.row_number().over(
        partition_by=ChatMessage.patient_id,
        order_by=ChatMessage.created_at.desc(),
    ).label("rn")
    last_ids = select(ChatMessage.id, rn).where(
        ChatMessage.patient_id.in_(list(stats))
    ).subquery()
    ids = list(db.scalars(select(last_ids.c.id).where(last_ids.c.rn == 1)))
    last_messages = {
        m.patient_id: m
        for m in db.scalars(select(ChatMessage).where(ChatMessage.id.in_(ids)))
    }
    fios = {
        u.id: (u.fio or u.email)
        for u in db.scalars(select(User).where(User.id.in_(list(stats))))
    }

    items = []
    for pid, (count, last_at) in stats.items():
        msg = last_messages.get(pid)
        preview = (msg.content or "").strip() if msg is not None else ""
        if len(preview) > CHAT_PREVIEW_MAX:
            preview = preview[: CHAT_PREVIEW_MAX - 1].rstrip() + "…"
        items.append({
            "patient_id": str(pid),
            "fio": fios.get(pid),
            "last_at": (msg.created_at if msg is not None else last_at).isoformat(),
            "last_sender": msg.sender if msg is not None else None,
            "preview": preview,
            "message_count": count,
        })
    items.sort(key=lambda i: i["last_at"], reverse=True)
    return items


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


def emias_summary(db: Session, clinician_id, patient_id, ip: str | None = None) -> dict:
    """Блок «ЕМИАС» в карточке врача (FR-25): связка, назначения, число приёмов.

    Roster-гейт + активное согласие пациента + аудит READ_EMIAS: история
    назначений — спецкатегория ПДн (152-ФЗ ст.9 ч.2-3). Отзыв согласия
    уничтожает данные (routers/consent._destroy_patient_pdn), поэтому до
    проверки согласия читать нечего и нельзя.

    Политика при CC_EMIAS_ENABLED=false: отдаём кэш (уже импортированные данные
    читаются локально), но с ``enabled=false`` — флаг управляет egress, а не
    чтением карты (P2-3 ревью; docs/04 §3.1, docs/09 P15).
    """
    from app.emias.config import current_config
    from app.emias.sync import patient_summary

    _require_roster(db, clinician_id, patient_id)
    active = db.scalar(
        select(Consent).where(Consent.user_id == patient_id, Consent.revoked_at.is_(None))
    )
    if active is None:
        raise Problem(403, "CONSENT_REQUIRED", "Нет активного согласия пациента — карта недоступна (152-ФЗ)")
    data = patient_summary(db, patient_id)
    audit_record(db, clinician_id, "READ_EMIAS", "patient", str(patient_id), ip=ip)
    db.commit()
    return {"enabled": current_config().enabled, **data}