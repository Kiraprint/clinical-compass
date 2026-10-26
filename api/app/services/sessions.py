"""FR-38/39: календарь приёмов и клинические сессии врача (docs/18 §3.1–3.3, §4.1–4.2).

Сервис держит три инварианта контракта:

1. Доступ — roster + активное согласие пациента на каждой пациентской операции;
   `patient_id` всегда берётся из строки БД (сессии/записи), а не из тела запроса
   (IDOR, docs/18 §2). Чужая сессия/запись неотличима от несуществующей.
2. Пересечение интервалов у одного врача запрещено (409 SLOT_CONFLICT). Проверка
   идёт в сервисе под `SELECT … FOR UPDATE` по строке врача: exclusion constraint
   (btree_gist) в контракте не вводится — docs/18 §3.1.
3. Завершение сессии создаёт ровно один `Visit` (идемпотентность по
   `visits.session_id`, docs/18 §3.3) — приём появляется в карточке пациента.

Аудит — только ids и структурные поля, без текста (docs/18 §2).

Ответы собираются в dict **до** `commit()`: после commit атрибуты ORM-объектов
истекли, а ленивая подгрузка в async-сессии запрещена (MissingGreenlet).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Problem
from app.models import (
    Appointment,
    ClinicalSession,
    Consent,
    DocumentArtifact,
    SessionProtocol,
    User,
    Visit,
)

from . import audit
from .clinician import require_patient_consent, require_roster

CHANNELS = ("in_person", "online", "phone")
TERMINAL = ("completed", "cancelled", "no_show")  # терминальны для планирования
MAX_WINDOW_DAYS = 62  # окно календаря/списка сессий (§4.1)
MAX_SLOT_MIN = 480  # потолок длительности приёма (§3.1) — окно проверки пересечений
PAGE_LIMIT = 200  # потолок пагинации API (§4)
SUMMARY_CHARS = 2000  # сводка подтверждённого протокола в notes приёма (§3.3)
# Статусы протокола, считающиеся «активной» версией (§3.4).
PROTOCOL_ACTIVE = ("draft", "approved", "signed", "submitted")
CONFIRMED = ("approved", "signed", "submitted")


def _page_limit(limit: int) -> int:
    """Потолок пагинации §4: клиент может попросить меньше 200, но не больше."""
    return PAGE_LIMIT if limit <= 0 or limit > PAGE_LIMIT else limit


def _consent_exists(patient_column):
    """EXISTS «у пациента активное согласие» — фильтр списков в SQL (docs/18 §2)."""
    return (
        select(Consent.id)
        .where(Consent.user_id == patient_column, Consent.revoked_at.is_(None))
        .exists()
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime, label: str) -> None:
    """ISO-8601 без зоны неоднозначен: naive-время отвергаем, а не угадываем пояс."""
    if value.tzinfo is None:
        raise Problem(
            422, "VALIDATION_ERROR", f"`{label}`: ожидается ISO-8601 с часовым поясом"
        )


def _window(from_: datetime, to: datetime) -> None:
    """Окно выборки: оба конца с поясом, `to >= from`, не шире 62 дней."""
    _aware(from_, "from")
    _aware(to, "to")
    if to < from_:
        raise Problem(422, "VALIDATION_ERROR", "`to` раньше `from`")
    if to - from_ > timedelta(days=MAX_WINDOW_DAYS):
        raise Problem(422, "VALIDATION_ERROR", f"Окно — не более {MAX_WINDOW_DAYS} дней")


async def _patients_fio(db: AsyncSession, ids) -> dict:
    """ФИО пациентов одним запросом (в ответах календаря и списка сессий)."""
    uniq = {i for i in ids if i is not None}
    if not uniq:
        return {}
    rows = (await db.execute(select(User.id, User.fio).where(User.id.in_(uniq)))).all()
    return {pid: fio for pid, fio in rows}


async def require_patient_access(db: AsyncSession, clinician_id, patient_id) -> None:
    """Roster + активное согласие пациента (docs/18 §2, существующие коды).

    Публичный гейт: его же вызывает сервис протоколов — доступ к протоколу не
    может быть шире доступа к сессии.
    """
    await require_roster(db, clinician_id, patient_id)
    await require_patient_consent(db, patient_id)


async def require_session(db: AsyncSession, clinician_id, session_id) -> ClinicalSession:
    """Сессия врача или 404 SESSION_NOT_FOUND (чужая — неотличима от несуществующей)."""
    session = await db.get(ClinicalSession, session_id)
    if session is None or session.clinician_id != clinician_id:
        raise Problem(404, "SESSION_NOT_FOUND", "Сессия не найдена")
    return session


async def _lock_clinician(db: AsyncSession, clinician_id) -> None:
    """Сериализация планирования одного врача: два параллельных POST не создадут
    пересекающиеся записи (row-lock вместо exclusion constraint, §3.1)."""
    await db.execute(select(User.id).where(User.id == clinician_id).with_for_update())


async def _overlaps(
    db: AsyncSession, clinician_id, starts_at: datetime, duration_min: int, exclude_id=None
) -> bool:
    """Есть ли у врача активная запись, пересекающаяся с [starts_at, +duration)."""
    end = starts_at + timedelta(minutes=duration_min)
    # Потолок длительности (480) ограничивает окно кандидатов сверху.
    candidates = (
        await db.scalars(
            select(Appointment).where(
                Appointment.clinician_id == clinician_id,
                Appointment.status.not_in(TERMINAL),
                Appointment.starts_at > starts_at - timedelta(minutes=MAX_SLOT_MIN),
                Appointment.starts_at < end,
            )
        )
    ).all()
    for other in candidates:
        if exclude_id is not None and other.id == exclude_id:
            continue
        other_end = other.starts_at + timedelta(minutes=other.duration_min)
        if other.starts_at < end and other_end > starts_at:
            return True
    return False


async def _appointments_with_session(db: AsyncSession, ids) -> set:
    uniq = [i for i in ids if i is not None]
    if not uniq:
        return set()
    rows = await db.scalars(
        select(ClinicalSession.appointment_id).where(ClinicalSession.appointment_id.in_(uniq))
    )
    return {r for r in rows if r is not None}


def _appointment_dict(a: Appointment, fio: str | None, has_session: bool) -> dict:
    return {
        "id": str(a.id),
        "patient_id": str(a.patient_id),
        "patient_fio": fio,
        "starts_at": a.starts_at.isoformat(),
        "duration_min": a.duration_min,
        "channel": a.channel,
        "status": a.status,
        "location": a.location,
        "has_session": has_session,
    }


async def list_appointments(
    db: AsyncSession,
    clinician_id,
    from_: datetime,
    to: datetime,
    patient_id=None,
    limit: int = PAGE_LIMIT,
    offset: int = 0,
) -> dict:
    """Календарь врача за окно (свой roster). `patient_id` — фильтр по пациенту."""
    _window(from_, to)
    if patient_id is not None:
        await require_patient_access(db, clinician_id, patient_id)
    stmt = select(Appointment).where(
        Appointment.clinician_id == clinician_id,
        Appointment.starts_at >= from_,
        Appointment.starts_at <= to,
    )
    if patient_id is not None:
        stmt = stmt.where(Appointment.patient_id == patient_id)
    stmt = stmt.where(_consent_exists(Appointment.patient_id))
    stmt = stmt.order_by(Appointment.starts_at).limit(_page_limit(limit)).offset(max(offset, 0))
    rows = (await db.scalars(stmt)).all()
    fio = await _patients_fio(db, [r.patient_id for r in rows])
    with_session = await _appointments_with_session(db, [r.id for r in rows])
    return {
        "items": [
            _appointment_dict(r, fio.get(r.patient_id), r.id in with_session) for r in rows
        ]
    }


async def _appointment(db: AsyncSession, clinician_id, appointment_id) -> Appointment:
    row = await db.get(Appointment, appointment_id)
    if row is None or row.clinician_id != clinician_id:
        raise Problem(404, "APPOINTMENT_NOT_FOUND", "Запись календаря не найдена")
    return row


async def create_appointment(
    db: AsyncSession,
    clinician_id,
    patient_id,
    starts_at: datetime,
    duration_min: int = 50,
    channel: str = "in_person",
    location: str | None = None,
    ip: str | None = None,
) -> dict:
    """Новая запись календаря: roster + согласие, 409 SLOT_CONFLICT на пересечении."""
    await require_patient_access(db, clinician_id, patient_id)
    if channel not in CHANNELS:
        raise Problem(422, "VALIDATION_ERROR", "Канал: in_person | online | phone")
    if not 10 <= duration_min <= 480:
        raise Problem(422, "VALIDATION_ERROR", "Длительность приёма: 10..480 минут")
    _aware(starts_at, "starts_at")
    await _lock_clinician(db, clinician_id)
    if await _overlaps(db, clinician_id, starts_at, duration_min):
        raise Problem(409, "SLOT_CONFLICT", "У врача уже есть приём в это время")
    row = Appointment(
        clinician_id=clinician_id,
        patient_id=patient_id,
        starts_at=starts_at,
        duration_min=duration_min,
        channel=channel,
        status="planned",
        location=location,
    )
    db.add(row)
    await db.flush()
    await audit.record(db, clinician_id, "APPOINTMENT_CREATE", "appointment", str(row.id), ip=ip)
    fio = await _patients_fio(db, [patient_id])
    payload = _appointment_dict(row, fio.get(patient_id), False)
    await db.commit()
    return payload


async def update_appointment(
    db: AsyncSession, clinician_id, appointment_id, changes: dict, ip: str | None = None
) -> dict:
    """Правка записи: терминальные статусы не перепланируются (409 APPOINTMENT_NOT_ACTIVE)."""
    row = await _appointment(db, clinician_id, appointment_id)
    await require_patient_access(db, clinician_id, row.patient_id)

    scheduling = {"starts_at", "duration_min", "status"} & set(changes)
    if row.status in TERMINAL and scheduling:
        raise Problem(
            409,
            "APPOINTMENT_NOT_ACTIVE",
            f"Запись в статусе {row.status} не перепланируется",
        )
    channel = changes.get("channel")
    if channel is not None and channel not in CHANNELS:
        raise Problem(422, "VALIDATION_ERROR", "Канал: in_person | online | phone")
    duration_min = changes.get("duration_min", row.duration_min)
    if not 10 <= duration_min <= 480:
        raise Problem(422, "VALIDATION_ERROR", "Длительность приёма: 10..480 минут")
    starts_at = changes.get("starts_at", row.starts_at)
    _aware(starts_at, "starts_at")

    if "starts_at" in changes or "duration_min" in changes:
        await _lock_clinician(db, clinician_id)
        if await _overlaps(db, clinician_id, starts_at, duration_min, exclude_id=row.id):
            raise Problem(409, "SLOT_CONFLICT", "У врача уже есть приём в это время")

    row.starts_at = starts_at
    row.duration_min = duration_min
    if channel is not None:
        row.channel = channel
    if "location" in changes:
        row.location = changes["location"]
    if "status" in changes:
        row.status = changes["status"]
    row.updated_at = _now()
    await audit.record(db, clinician_id, "APPOINTMENT_UPDATE", "appointment", str(row.id), ip=ip)
    await db.flush()
    fio = await _patients_fio(db, [row.patient_id])
    has_session = bool(await _appointments_with_session(db, [row.id]))
    payload = _appointment_dict(row, fio.get(row.patient_id), has_session)
    await db.commit()
    return payload


async def _protocol_brief(db: AsyncSession, session_id) -> dict | None:
    """Активная версия протокола для ответов Session: `{version, status}` | null."""
    row = await db.scalar(
        select(SessionProtocol)
        .where(
            SessionProtocol.session_id == session_id,
            SessionProtocol.status.in_(PROTOCOL_ACTIVE),
        )
        .order_by(SessionProtocol.version.desc())
        .limit(1)
    )
    return {"version": row.version, "status": row.status} if row else None


async def _session_dict(db: AsyncSession, s: ClinicalSession, fio: str | None = None) -> dict:
    if fio is None:
        fio = (await _patients_fio(db, [s.patient_id])).get(s.patient_id)
    return {
        "id": str(s.id),
        "appointment_id": str(s.appointment_id) if s.appointment_id else None,
        "patient_id": str(s.patient_id),
        "patient_fio": fio,
        "channel": s.channel,
        "started_at": s.started_at.isoformat(),
        "ended_at": s.ended_at.isoformat() if s.ended_at else None,
        "status": s.status,
        "audio_consent": s.audio_consent,
        "ai_status": s.ai_status,
        "ai_error": s.ai_error,
        "protocol": await _protocol_brief(db, s.id),
    }


async def _session_by_appointment(db: AsyncSession, appointment_id):
    return await db.scalar(
        select(ClinicalSession).where(ClinicalSession.appointment_id == appointment_id)
    )


async def _start(
    db: AsyncSession,
    clinician_id,
    patient_id,
    appointment: Appointment | None,
    channel: str,
    audio_consent: bool,
    ip: str | None,
) -> tuple[dict, bool]:
    """Создать сессию (или вернуть существующую для этой записи) + аудит старта."""
    existing = await _session_by_appointment(db, appointment.id) if appointment else None
    if existing is not None:
        return await _session_dict(db, existing), False
    session = ClinicalSession(
        appointment_id=appointment.id if appointment else None,
        patient_id=patient_id,  # всегда из записи/аргумента пациента, не из тела
        clinician_id=clinician_id,
        channel=channel,
        started_at=_now(),
        audio_consent=audio_consent,
    )
    db.add(session)
    try:
        await db.flush()
    except IntegrityError:
        # Гонка двух start: partial unique по appointment_id уже создал сессию.
        await db.rollback()
        existing = await _session_by_appointment(db, appointment.id) if appointment else None
        if existing is None:
            raise
        return await _session_dict(db, existing), False
    await audit.record(
        db, clinician_id, "SESSION_START", "clinical_session", str(session.id), ip=ip
    )
    payload = await _session_dict(db, session)
    await db.commit()
    return payload, True


async def start_appointment(
    db: AsyncSession, clinician_id, appointment_id, ip: str | None = None
) -> tuple[dict, bool]:
    """Старт приёма по записи календаря: повторный вызов возвращает ту же сессию."""
    appointment = await _appointment(db, clinician_id, appointment_id)
    await require_patient_access(db, clinician_id, appointment.patient_id)
    if appointment.status in TERMINAL:
        raise Problem(
            409,
            "APPOINTMENT_NOT_ACTIVE",
            f"Запись в статусе {appointment.status} — приём не начинается",
        )
    return await _start(
        db,
        clinician_id,
        appointment.patient_id,
        appointment,
        appointment.channel,
        False,
        ip,
    )


async def create_session(
    db: AsyncSession,
    clinician_id,
    patient_id,
    channel: str = "in_person",
    appointment_id=None,
    audio_consent: bool = False,
    ip: str | None = None,
) -> tuple[dict, bool]:
    """Сессия вне плана (`appointment_id=None`) или по записи календаря."""
    await require_patient_access(db, clinician_id, patient_id)
    if channel not in CHANNELS:
        raise Problem(422, "VALIDATION_ERROR", "Канал: in_person | online | phone")
    appointment = None
    if appointment_id is not None:
        appointment = await _appointment(db, clinician_id, appointment_id)
        if appointment.patient_id != patient_id:
            raise Problem(
                422, "VALIDATION_ERROR", "Запись календаря относится к другому пациенту"
            )
        if appointment.status in TERMINAL:
            raise Problem(
                409,
                "APPOINTMENT_NOT_ACTIVE",
                f"Запись в статусе {appointment.status} — приём не начинается",
            )
        channel = appointment.channel
    return await _start(
        db, clinician_id, patient_id, appointment, channel, audio_consent, ip
    )


async def list_sessions(
    db: AsyncSession,
    clinician_id,
    from_: datetime,
    to: datetime,
    patient_id=None,
    status: str | None = None,
    limit: int = PAGE_LIMIT,
    offset: int = 0,
) -> dict:
    """Сессии врача за окно; `status` — active | finished."""
    _window(from_, to)
    if status is not None and status not in ("active", "finished"):
        raise Problem(422, "VALIDATION_ERROR", "Статус: active | finished")
    if patient_id is not None:
        await require_patient_access(db, clinician_id, patient_id)
    stmt = select(ClinicalSession).where(
        ClinicalSession.clinician_id == clinician_id,
        ClinicalSession.started_at >= from_,
        ClinicalSession.started_at <= to,
    )
    if patient_id is not None:
        stmt = stmt.where(ClinicalSession.patient_id == patient_id)
    if status is not None:
        stmt = stmt.where(ClinicalSession.status == status)
    stmt = stmt.where(_consent_exists(ClinicalSession.patient_id))
    stmt = stmt.order_by(ClinicalSession.started_at).limit(_page_limit(limit))
    stmt = stmt.offset(max(offset, 0))
    rows = (await db.scalars(stmt)).all()
    fio = await _patients_fio(db, [r.patient_id for r in rows])
    return {"items": [await _session_dict(db, r, fio.get(r.patient_id)) for r in rows]}


async def _artifacts(db: AsyncSession, session_id) -> list[dict]:
    rows = (
        await db.scalars(
            select(DocumentArtifact)
            .where(DocumentArtifact.session_id == session_id)
            .order_by(DocumentArtifact.created_at)
        )
    ).all()
    return [
        {
            "id": str(a.id),
            "kind": a.kind,
            "mime": a.mime,
            "size_bytes": a.size_bytes,
            "sha256": a.sha256,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


async def get_session(db: AsyncSession, clinician_id, session_id) -> dict:
    """SessionDetail: сессия + протокол + артефакты + подпись/передача (P4/P5 → null)."""
    session = await require_session(db, clinician_id, session_id)
    await require_patient_access(db, clinician_id, session.patient_id)
    payload = await _session_dict(db, session)
    protocol = await db.scalar(
        select(SessionProtocol)
        .where(
            SessionProtocol.session_id == session.id,
            SessionProtocol.status.in_(PROTOCOL_ACTIVE),
        )
        .order_by(SessionProtocol.version.desc())
        .limit(1)
    )
    payload["protocol"] = (
        {"id": str(protocol.id), "version": protocol.version, "status": protocol.status}
        if protocol
        else None
    )
    payload["artifacts"] = await _artifacts(db, session.id)
    # §4.2: подпись (PR P4) и передача в ЕМИАС (PR P5) появятся позже — контракт
    # ответа фиксирован заранее, поэтому ключи есть и сейчас.
    payload["signature"] = None
    payload["delivery"] = None
    return payload


async def update_session(
    db: AsyncSession, clinician_id, session_id, changes: dict, ip: str | None = None
) -> dict:
    """PATCH сессии: на текущем PR — только `audio_consent` (docs/18 §4.2)."""
    session = await require_session(db, clinician_id, session_id)
    await require_patient_access(db, clinician_id, session.patient_id)
    if "audio_consent" in changes:
        session.audio_consent = bool(changes["audio_consent"])
        # Согласие на запись — отдельное основание обработки спецкатегории (§1.3):
        # каждое переключение фиксируется в append-only журнале (только id).
        await audit.record(
            db, clinician_id, "SESSION_CONSENT_UPDATE", "clinical_session", str(session.id),
            ip=ip,
        )
    await db.flush()
    payload = await _session_dict(db, session)
    await db.commit()
    return payload


async def _summary(db: AsyncSession, session_id) -> str:
    """Сводка подтверждённого протокола для notes приёма (≤ 2000 символов, §3.3)."""
    protocol = await db.scalar(
        select(SessionProtocol)
        .where(
            SessionProtocol.session_id == session_id,
            SessionProtocol.status.in_(CONFIRMED),
        )
        .order_by(SessionProtocol.version.desc())
        .limit(1)
    )
    if protocol is None:
        return ""
    from . import protocols  # локальный импорт: protocols зависит от этого модуля

    sections = protocols.decode_sections(protocol)
    parts: list[str] = []
    if sections.get("complaints"):
        parts.append(f"Жалобы: {sections['complaints']}")
    mental = sections.get("mental_status") or {}
    if mental.get("suicidal_risk"):
        parts.append(f"Суицидальный риск: {mental['suicidal_risk']}")
    code = (sections.get("diagnosis") or {}).get("icd10")
    if code:
        parts.append(f"Диагноз: {code}")
    if sections.get("recommendations"):
        parts.append(f"Рекомендации: {sections['recommendations']}")
    return "\n".join(parts)[:SUMMARY_CHARS]


async def finish_session(
    db: AsyncSession,
    clinician_id,
    session_id,
    duration_min: int | None = None,
    ip: str | None = None,
) -> dict:
    """Завершить сессию и создать приём (идемпотентно по `visits.session_id`)."""
    session = await require_session(db, clinician_id, session_id)
    await require_patient_access(db, clinician_id, session.patient_id)
    if duration_min is not None and not 1 <= duration_min <= 480:
        raise Problem(422, "VALIDATION_ERROR", "Длительность приёма: 1..480 минут")

    visit = await db.scalar(select(Visit).where(Visit.session_id == session.id))
    if session.status == "finished":
        payload = await _session_dict(db, session)
        payload["visit_id"] = str(visit.id) if visit else None
        payload["duration_min"] = visit.duration_min if visit else None
        return payload

    ended_at = _now()
    minutes = duration_min
    if minutes is None:
        # Потолок visits.duration_min (1..480): длинная сессия не должна ронять finish.
        minutes = min(MAX_SLOT_MIN, max(1, round((ended_at - session.started_at).total_seconds() / 60)))
    session.ended_at = ended_at
    session.status = "finished"

    try:
        if visit is None:
            visit = Visit(
                patient_id=session.patient_id,
                clinician_id=clinician_id,
                visited_at=session.started_at,
                visit_type=session.channel,
                duration_min=minutes,
                notes=await _summary(db, session.id),
                session_id=session.id,
            )
            db.add(visit)
        await db.flush()  # UNIQUE visits.session_id: гонку двух finish ловим ниже
    except IntegrityError:
        # Параллельный finish уже создал приём: отдаём его, второй не нужен.
        await db.rollback()
        session = await require_session(db, clinician_id, session_id)
        visit = await db.scalar(select(Visit).where(Visit.session_id == session.id))
        payload = await _session_dict(db, session)
        payload["visit_id"] = str(visit.id) if visit else None
        payload["duration_min"] = visit.duration_min if visit else None
        return payload

    await audit.record(
        db, clinician_id, "SESSION_FINISH", "clinical_session", str(session.id), ip=ip
    )
    await db.flush()
    payload = await _session_dict(db, session)
    payload["visit_id"] = str(visit.id)
    payload["duration_min"] = minutes
    await db.commit()
    return payload
