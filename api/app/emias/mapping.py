"""Маппинг ресурсов ЕМИАС → локальные модели (FR-25). Только чистые функции.

Идентификация пациента. В контракте ЕМИАС сильный идентификатор один — СНИЛС
(identifier с OID 1.2.643.100.3); email/dob у нас не совпадают с ЕМИАС
(email в ЕМИАС нет, даты рождения в users нет), поэтому:
  1) основной путь — совпадение `sha256(СНИЛС + перец)` с emias_links.snils_hash
     (идемпотентность повторных синхронизаций);
  2) fallback — точное совпадение нормализованного ФИО ровно с одним локальным
     пациентом. Это слабый признак, поэтому он НЕ создаёт пользователей: без
     однозначного совпадения запись пропускается (skipped=no_local_match),
     а связка фиксируется только вместе с найденным пациентом.

СНИЛС в открытом виде не хранится нигде, включая логи и stats прогона.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.models import EmiasLink, User

SNILS_SYSTEM = "urn:oid:1.2.643.100.3"
SNILS_RE = re.compile(r"\D")

VISIT_TYPE_BY_CLASS = {
    "AMB": "in_person",   # амбулаторный приём
    "IMP": "in_person",   # стационар
    "EMER": "in_person",
    "HH": "online",       # home health / телемедицина
    "VR": "online",
    "ONLINE": "online",
    "PHONE": "phone",
}

MEDICATION_STATUS = {
    "active": "active",
    "completed": "completed",
    "stopped": "stopped",
    "cancelled": "stopped",
    "entered-in-error": "stopped",
}

def normalize_snils(raw: str | None) -> str:
    """Только цифры. СНИЛС — 11 цифр (152-ФЗ: спецкатегория не публикуется)."""
    digits = SNILS_RE.sub("", raw or "")
    if len(digits) != 11:
        raise ValueError("СНИЛС должен содержать 11 цифр")
    return digits

def snils_hash(snils: str, pepper: str) -> str:
    """sha256(СНИЛС:перец) — поиск без хранения идентификатора в открытом виде."""
    normalized = normalize_snils(snils)
    return hashlib.sha256(f"{normalized}:{pepper}".encode("utf-8")).hexdigest()

def extract_snils(patient: dict) -> str | None:
    for identifier in patient.get("identifier") or []:
        if not isinstance(identifier, dict):
            continue
        system = (identifier.get("system") or "").lower()
        kind = ((identifier.get("type") or {}).get("text") or "").upper()
        if system == SNILS_SYSTEM.lower() or kind == "SNILS":
            value = identifier.get("value")
            if value:
                return str(value)
    return None

def patient_display_name(patient: dict) -> str:
    names = patient.get("name") or []
    if not names or not isinstance(names[0], dict):
        return ""
    name = names[0]
    family = (name.get("family") or "").strip()
    given = " ".join(g for g in (name.get("given") or []) if isinstance(g, str)).strip()
    return " ".join(part for part in (family, given) if part)

def normalize_fio(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()

def find_local_patient(
    db: Session, *, snils_hash_value: str | None, fio: str | None
) -> tuple[User | None, str | None]:
    """Локальный пациент для ресурса FHIR. Возвращает (user, 'snils'|'fio'|None)."""
    if snils_hash_value:
        linked = db.scalar(
            select(User)
            .join(EmiasLink, EmiasLink.patient_id == User.id)
            .where(EmiasLink.snils_hash == snils_hash_value)
        )
        if linked is not None:
            return linked, "snils"
    normalized = normalize_fio(fio)
    if normalized:
        rows = db.scalars(
            select(User).where(
                User.role == "PATIENT",
                func.lower(func.trim(User.fio)) == normalized,
                ~exists().where(EmiasLink.patient_id == User.id),
            )
        ).all()
        if len(rows) == 1:
            return rows[0], "fio"
    return None, None

# --- Encounter → Visit ------------------------------------------------------

def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None

def encounter_class_code(encounter: dict) -> str:
    klass = encounter.get("class") or {}
    if isinstance(klass, list):
        klass = klass[0] if klass else {}
    return (klass.get("code") or "").upper()

def encounter_visit_type(encounter: dict) -> str:
    return VISIT_TYPE_BY_CLASS.get(encounter_class_code(encounter), "in_person")

def _first_text(items) -> str:
    if not isinstance(items, list):
        return ""
    for item in items:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if text:
            return str(text).strip()
        for coding in item.get("coding") or []:
            if isinstance(coding, dict) and coding.get("display"):
                return str(coding["display"]).strip()
    return ""

def encounter_notes(encounter: dict) -> str:
    """Текст визита: причина + врач + учреждение. ПДн → EncryptedText в модели."""
    provider = ((encounter.get("serviceProvider") or {}).get("display") or "").strip()
    reason = _first_text(encounter.get("reasonCode"))
    participants = encounter.get("participant") or []
    doctor = ""
    if participants and isinstance(participants[0], dict):
        doctor = ((participants[0].get("individual") or {}).get("display") or "").strip()
    parts = [p for p in (reason, f"врач: {doctor}" if doctor else "", provider) if p]
    return "ЕМИАС: " + "; ".join(parts) if parts else "Приём в ЕМИАС"

def encounter_visit_fields(encounter: dict) -> dict:
    """Поля Visit (кроме patient_id): idempotентный upsert по emias_id."""
    period = encounter.get("period") or {}
    start = _parse_dt(period.get("start"))
    end = _parse_dt(period.get("end"))
    duration = None
    if start and end:
        minutes = round((end - start).total_seconds() / 60)
        if 1 <= minutes <= 480:
            duration = minutes
    if start is None:
        raise ValueError("Encounter.period.start отсутствует")
    return {
        "emias_id": str(encounter.get("id") or ""),
        "visited_at": start,
        "visit_type": encounter_visit_type(encounter),
        "duration_min": duration,
        "notes": encounter_notes(encounter),
        "source": "emias",
    }

def encounter_summary(encounter: dict) -> dict:
    """Компактная карточка для UI (без свободного текста, кроме причины)."""
    fields = encounter_visit_fields(encounter)
    return {
        "emias_id": fields["emias_id"],
        "visited_at": fields["visited_at"].isoformat(),
        "visit_type": fields["visit_type"],
        "duration_min": fields["duration_min"],
        "reason": _first_text(encounter.get("reasonCode")),
        "provider": ((encounter.get("serviceProvider") or {}).get("display") or "").strip(),
    }

# --- MedicationRequest → PatientMedication ---------------------------------

def medication_fields(request: dict) -> dict:
    concept = request.get("medicationCodeableConcept") or {}
    title = (concept.get("text") or "").strip() or _first_text([concept])
    if not title:
        raise ValueError("MedicationRequest без названия препарата")
    dosage = ""
    instructions = request.get("dosageInstruction") or []
    if instructions and isinstance(instructions[0], dict):
        dosage = (instructions[0].get("text") or "").strip()
    emias_id = str(request.get("id") or "")
    if not emias_id:
        raise ValueError("MedicationRequest без id")
    return {
        "emias_id": emias_id,
        "title": title,
        "dosage": dosage or None,
        "started_on": _parse_date(request.get("authoredOn")),
        "status": MEDICATION_STATUS.get((request.get("status") or "").lower(), "unknown"),
        # FR-23: в одной таблице живут импорт ЕМИАС и локальные назначения —
        # импорт всегда помечается, иначе строка выглядит как назначение врача.
        "source": "emias",
    }

def medication_dict(med) -> dict:
    """ORM PatientMedication → JSON для витрины врача."""
    return {
        "id": str(med.id),
        "title": med.title,
        "dosage": med.dosage,
        "started_on": med.started_on.isoformat() if med.started_on else None,
        "status": med.status,
        # emias_id — внутренний идентификатор контура: врачу не нужен (P3 ревью).
    }
