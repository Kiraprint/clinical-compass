"""Единая хронология карточки пациента (FR-23, «История пациента»).

Сливает в одну ленту приёмы, назначения препаратов, скрининги и нежелательные
явления. Доступ — roster + активное согласие (152-ФЗ), аудит READ_PATIENT_HISTORY.
Один SQL-запрос на тип (LIMIT), без N+1; PHI-текст расшифровывается ORM
(EncryptedText) только на стороне сервера.

Курсор `before` — непрозрачная base64url-строка от "<ISO-время>|<uuid>",
сравнение по паре (at, id): время может совпадать (created_at в одну секунду),
id разрешает ничью. base64url — чтобы '+' смещения UTC не ломался в query.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Problem
from app.models import AdverseEvent, Assessment, PatientMedication, Run, Visit

from .audit import record as audit_record
from .clinician import require_patient_consent, require_roster

HISTORY_TYPES = ("visit", "medication", "screening", "adverse")

VISIT_LABELS = {"in_person": "очный", "online": "онлайн", "phone": "телефон"}
MED_STATUS_LABELS = {"active": "принимает", "completed": "курс завершён", "stopped": "остановлено", "unknown": "статус неизвестен"}
SUMMARY_LIMIT = 200

def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= SUMMARY_LIMIT else text[: SUMMARY_LIMIT - 1] + "…"

def _visit_item(v: Visit) -> dict:
    parts = [VISIT_LABELS.get(v.visit_type, v.visit_type)]
    if v.duration_min:
        parts.append(f"{v.duration_min} мин")
    summary = ", ".join(parts)
    if v.notes:
        summary = f"{summary}: {v.notes}"
    return {
        "type": "visit",
        "title": "Приём",
        "summary": _clip(summary),
        "ref_id": str(v.id),
        "source": "emias" if v.source == "emias" else "clinician",
    }

def _medication_item(m: PatientMedication) -> dict:
    parts = []
    if m.dosage:
        parts.append(m.dosage)
    parts.append(MED_STATUS_LABELS.get(m.status, m.status))
    if m.ended_on:
        parts.append(f"до {m.ended_on.isoformat()}")
    if m.notes:
        parts.append(m.notes)
    return {
        "type": "medication",
        "title": m.title,
        "summary": _clip("; ".join(parts)),
        "ref_id": str(m.id),
        "source": m.source,
    }

def _screening_item(a: Assessment, questionnaire_code: str | None) -> dict:
    code = questionnaire_code or "скрининг"
    return {
        "type": "screening",
        "title": f"Скрининг {code}",
        "summary": _clip(f"{a.total_score} баллов, {a.severity}, триаж {a.triage}"),
        "ref_id": str(a.id),
        # скрининг заполняет пациент, не врач
        "source": "patient",
    }

def _adverse_item(e: AdverseEvent) -> dict:
    return {
        "type": "adverse",
        "title": f"Нежелательное явление ({e.severity})",
        "summary": _clip(e.event_text),
        "ref_id": str(e.id),
        "source": "clinician",
    }

def _encode_cursor(at: datetime, ref_id: uuid.UUID) -> str:
    raw = f"{at.isoformat()}|{ref_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def _parse_cursor(raw: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        at_raw, id_raw = base64.urlsafe_b64decode(padded).decode().rsplit("|", 1)
        at = datetime.fromisoformat(at_raw)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return at, uuid.UUID(id_raw)
    except (ValueError, AttributeError, binascii.Error, UnicodeDecodeError) as exc:
        raise Problem(422, "VALIDATION", "Некорректный курсор before") from exc

def _parse_types(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return HISTORY_TYPES
    requested = tuple(dict.fromkeys(t.strip() for t in raw.split(",") if t.strip()))
    unknown = [t for t in requested if t not in HISTORY_TYPES]
    if unknown:
        raise Problem(422, "VALIDATION", f"Неизвестный тип: {', '.join(unknown)}")
    return requested

def _older_than(at_col, id_col, cursor: tuple[datetime, uuid.UUID]):
    """Строго старше курсора по паре (at, id) — предикат уходит в SQL, поэтому
    LIMIT считает только непросмотренные строки (в т.ч. при совпадающих at)."""
    at, ref_id = cursor
    return or_(at_col < at, and_(at_col == at, id_col < ref_id))

async def patient_history(
    db: AsyncSession, clinician_id, patient_id, *,
    types: str | None = None, limit: int = 50, before: str | None = None,
    ip: str | None = None,
) -> dict:
    """Слитая лента событий карточки, новые первыми. Roster + consent + аудит."""
    await require_roster(db, clinician_id, patient_id)
    await require_patient_consent(db, patient_id)
    wanted = _parse_types(types)
    cursor = _parse_cursor(before) if before else None
    fetch = limit + 1  # +1 — чтобы понять, есть ли следующая страница

    merged: list[tuple[datetime, uuid.UUID, dict]] = []

    if "visit" in wanted:
        q = select(Visit).where(Visit.patient_id == patient_id)
        if cursor is not None:
            q = q.where(_older_than(Visit.visited_at, Visit.id, cursor))
        q = q.order_by(Visit.visited_at.desc(), Visit.id.desc()).limit(fetch)
        merged += [(v.visited_at, v.id, _visit_item(v)) for v in await db.scalars(q)]

    if "medication" in wanted:
        q = select(PatientMedication).where(PatientMedication.patient_id == patient_id)
        if cursor is not None:
            q = q.where(_older_than(PatientMedication.created_at, PatientMedication.id, cursor))
        q = q.order_by(PatientMedication.created_at.desc(), PatientMedication.id.desc()).limit(fetch)
        merged += [(m.created_at, m.id, _medication_item(m)) for m in await db.scalars(q)]

    if "screening" in wanted:
        q = (
            select(Assessment, Run.questionnaire_code)
            .join(Run, Run.id == Assessment.run_id)
            .where(Assessment.patient_id == patient_id)
        )
        if cursor is not None:
            q = q.where(_older_than(Assessment.computed_at, Assessment.id, cursor))
        q = q.order_by(Assessment.computed_at.desc(), Assessment.id.desc()).limit(fetch)
        merged += [(a.computed_at, a.id, _screening_item(a, code)) for a, code in await db.execute(q)]

    if "adverse" in wanted:
        q = select(AdverseEvent).where(AdverseEvent.patient_id == patient_id)
        if cursor is not None:
            q = q.where(_older_than(AdverseEvent.created_at, AdverseEvent.id, cursor))
        q = q.order_by(AdverseEvent.created_at.desc(), AdverseEvent.id.desc()).limit(fetch)
        merged += [(e.created_at, e.id, _adverse_item(e)) for e in await db.scalars(q)]

    merged.sort(key=lambda row: (row[0], row[1]), reverse=True)

    page = merged[:limit]
    next_before = None
    if len(merged) > limit:
        last_at, last_id, _ = page[-1]
        next_before = _encode_cursor(last_at, last_id)

    items = []
    for at, _, item in page:
        item["at"] = at.isoformat()
        items.append(item)

    await audit_record(db, clinician_id, "READ_PATIENT_HISTORY", "patient", str(patient_id), ip=ip)
    await db.commit()
    return {"items": items, "next_before": next_before, "types": list(wanted)}
