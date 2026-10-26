"""FR-39: протокол приёма — структура §5, валидация §5.2, версии §3.4, approve §4.4.

Протокол — юридически значимая запись приёма (docs/18 §1.1), а не заметка ИИ:
черновик создаёт и подтверждает врач, ИИ (PR P3) заполняет только нарративные
разделы и никогда — `diagnosis`/`treatment`.

Инварианты версий (§3.4):
- одновременно активна максимум одна версия из `draft|approved|signed|submitted`;
- правка draft — на месте; approved/submitted — 409 PROTOCOL_LOCKED;
- правка signed создаёт новую версию (предыдущая → `superseded`, подпись не
  переносится — требуется повторная, PR P4).

`sections` хранится каноническим JSON (§5.2) в `EncryptedText`: в БД только
шифротекст. `content_sha256` считается по тем же байтам, что будут подписаны.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Problem
from app.models import SessionProtocol

from . import audit
from .sessions import PROTOCOL_ACTIVE, require_patient_access, require_session

# §5.2: потолки разделов. Ключ — путь раздела, значение — символов.
LIMITS = {
    "complaints": 4000,
    "anamnesis_morbi": 8000,
    "somatic_status": 2000,
    "recommendations": 4000,
}
MENTAL_CHARS = 2000
MENTAL_FIELDS = (
    "appearance_behavior",
    "consciousness_orientation",
    "perception",
    "thinking_speech",
    "emotions_volition",
    "criticism",
    "suicidal_risk",
)
RATIONALE_CHARS = 2000
TREATMENT_MAX = 20  # позиций назначений
LINE_CHARS = 500  # потолок одной строки (паспорт, назначение)
PASSPORT_FIELDS = ("visit_at", "channel", "legal_basis")
ICD10 = re.compile(r"^[A-Z]\d{2}(\.\d{1,2})?$")

# §5.2: обязательны для approve (пусто → 409 PROTOCOL_NOT_READY со списком).
REQUIRED = (
    ("passport.visit_at", ("passport", "visit_at")),
    ("passport.legal_basis", ("passport", "legal_basis")),
    ("complaints", ("complaints",)),
    ("mental_status.suicidal_risk", ("mental_status", "suicidal_risk")),
    ("diagnosis.icd10", ("diagnosis", "icd10")),
)


def canonical(sections: dict) -> str:
    """Канонический JSON §5.2 — именно эти байты хэшируются и подписываются."""
    return json.dumps(sections, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_sections(sections: dict) -> str:
    return canonical(sections)


def decode_sections(row: SessionProtocol) -> dict:
    """Расшифрованные разделы версии (пустое/битое значение → {})."""
    raw = row.sections_cipher
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}

def content_sha256(sections: dict) -> str:
    return hashlib.sha256(canonical(sections).encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _too_long(path: str, limit: int) -> Problem:
    return Problem(
        422, "PROTOCOL_SECTION_TOO_LONG", f"{path}: максимум {limit} символов"
    )


def _check_text(value, path: str, limit: int) -> None:
    """Строковое поле не длиннее лимита; не-строка → 422 (схема §5.1)."""
    if value is None:
        return
    if not isinstance(value, str):
        raise Problem(422, "VALIDATION_ERROR", f"{path}: ожидалась строка")
    if len(value) > limit:
        raise _too_long(path, limit)


def validate_sections(sections: dict) -> None:
    """Потолки §5.2 и формат МКБ-10. Пустые разделы допустимы до approve."""
    if not isinstance(sections, dict):
        raise Problem(422, "VALIDATION_ERROR", "sections: ожидался объект")
    for name, limit in LIMITS.items():
        _check_text(sections.get(name), name, limit)

    mental = sections.get("mental_status")
    if mental is not None:
        if not isinstance(mental, dict):
            raise Problem(422, "VALIDATION_ERROR", "mental_status: ожидался объект")
        for field in MENTAL_FIELDS:
            _check_text(mental.get(field), f"mental_status.{field}", MENTAL_CHARS)

    passport = sections.get("passport")
    if passport is not None:
        if not isinstance(passport, dict):
            raise Problem(422, "VALIDATION_ERROR", "passport: ожидался объект")
        for field in PASSPORT_FIELDS:
            _check_text(passport.get(field), f"passport.{field}", LINE_CHARS)

    diagnosis = sections.get("diagnosis")
    if diagnosis is not None:
        if not isinstance(diagnosis, dict):
            raise Problem(422, "VALIDATION_ERROR", "diagnosis: ожидался объект")
        _check_text(diagnosis.get("syndrome"), "diagnosis.syndrome", LINE_CHARS)
        _check_text(diagnosis.get("rationale"), "diagnosis.rationale", RATIONALE_CHARS)
        code = diagnosis.get("icd10")
        if isinstance(code, str) and code.strip() and not ICD10.match(code.strip()):
            raise Problem(
                422, "PROTOCOL_ICD10_INVALID", "diagnosis.icd10: ожидается код МКБ-10 (F41.1)"
            )
        if code is not None and not isinstance(code, str):
            raise Problem(422, "VALIDATION_ERROR", "diagnosis.icd10: ожидалась строка")

    treatment = sections.get("treatment")
    if treatment is not None:
        if not isinstance(treatment, list):
            raise Problem(422, "VALIDATION_ERROR", "treatment: ожидался список")
        if len(treatment) > TREATMENT_MAX:
            raise Problem(
                422, "PROTOCOL_SECTION_TOO_LONG", f"treatment: максимум {TREATMENT_MAX} позиций"
            )
        for i, item in enumerate(treatment):
            if not isinstance(item, dict):
                raise Problem(422, "VALIDATION_ERROR", f"treatment[{i}]: ожидался объект")
            for field in ("drug", "dose", "schedule", "duration"):
                _check_text(item.get(field), f"treatment[{i}].{field}", LINE_CHARS)


def _value(sections: dict, path: tuple[str, ...]):
    node = sections
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def missing_required(sections: dict) -> list[str]:
    """Список незаполненных обязательных разделов (§5.2) — для detail 409."""
    return [
        name
        for name, path in REQUIRED
        if not (isinstance(_value(sections, path), str) and _value(sections, path).strip())
    ]


def changed_sections(old: dict, new: dict) -> list[str]:
    """Имена изменённых разделов (без текста — только ключи §5.1)."""
    keys = set(old) | set(new)
    changed = []
    for key in sorted(keys):
        if key == "mental_status":
            before = old.get(key) or {}
            after = new.get(key) or {}
            if {f for f in MENTAL_FIELDS if (before.get(f) or "") != (after.get(f) or "")}:
                changed.append(key)
            continue
        if canonical({key: old.get(key)}) != canonical({key: new.get(key)}):
            changed.append(key)
    return changed


def _payload(row: SessionProtocol) -> dict:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "version": row.version,
        "status": row.status,
        "source": row.source,
        "sections": decode_sections(row),
        "content_sha256": row.content_sha256,
        "diagnosis_code": row.diagnosis_code,
        "model": row.model,
        "prompt_version": row.prompt_version,
        "needs_review": row.needs_review,
        "created_by": str(row.created_by),
        "approved_by": str(row.approved_by) if row.approved_by else None,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _active(db: AsyncSession, session_id) -> SessionProtocol | None:
    return await db.scalar(
        select(SessionProtocol)
        .where(
            SessionProtocol.session_id == session_id,
            SessionProtocol.status.in_(PROTOCOL_ACTIVE),
        )
        .order_by(SessionProtocol.version.desc())
        .limit(1)
    )


def _check_code(code: str | None) -> str | None:
    if code is None:
        return None
    cleaned = code.strip()
    if not cleaned:
        return None
    if not ICD10.match(cleaned):
        raise Problem(
            422, "PROTOCOL_ICD10_INVALID", "diagnosis_code: ожидается код МКБ-10 (F41.1)"
        )
    return cleaned


async def get(db: AsyncSession, clinician_id, session_id) -> dict:
    """Текущая версия протокола сессии; нет ни одной — 404 PROTOCOL_NOT_FOUND."""
    session = await require_session(db, clinician_id, session_id)
    await require_patient_access(db, clinician_id, session.patient_id)
    row = await _active(db, session.id)
    if row is None:
        raise Problem(404, "PROTOCOL_NOT_FOUND", "Протокол ещё не заполнен")
    return _payload(row)


async def put(
    db: AsyncSession,
    clinician_id,
    session_id,
    sections: dict,
    diagnosis_code: str | None = None,
    ip: str | None = None,
) -> dict:
    """Сохранить разделы врача: draft — на месте, signed — новая версия, иначе 409."""
    session = await require_session(db, clinician_id, session_id)
    await require_patient_access(db, clinician_id, session.patient_id)
    validate_sections(sections)
    code = _check_code(diagnosis_code)

    active = await _active(db, session.id)
    if active is None:
        row = SessionProtocol(
            session_id=session.id,
            version=1,
            status="draft",
            source="manual",
            sections_cipher=encode_sections(sections),
            diagnosis_code=code,
            created_by=clinician_id,
        )
        db.add(row)
        await db.flush()
        changed = sorted(sections)
    else:
        if active.status == "approved":
            raise Problem(409, "PROTOCOL_LOCKED", "Протокол подтверждён — правка запрещена")
        if active.status == "submitted":
            raise Problem(409, "PROTOCOL_LOCKED", "Протокол передан в ЕМИАС — правка запрещена")
        if active.status == "signed":
            # Подпись не переносится на новые байты: новая версия + повторная подпись.
            row = SessionProtocol(
                session_id=session.id,
                version=active.version + 1,
                status="draft",
                source="manual",
                sections_cipher=encode_sections(sections),
                diagnosis_code=code,
                created_by=clinician_id,
            )
            active.status = "superseded"
            db.add(row)
            await db.flush()
            changed = sorted(sections)
        else:  # draft
            previous = decode_sections(active)
            changed = changed_sections(previous, sections)
            active.sections_cipher = encode_sections(sections)
            if code is not None:
                active.diagnosis_code = code
            active.content_sha256 = None  # approve пересчитает хэш новых байтов
            row = active

    # Метаданные аудита ограничены колонками audit_log: вид объекта + id версии
    # (текст разделов в журнал не попадает — docs/18 §2).
    await audit.record(
        db,
        clinician_id,
        "SESSION_PROTOCOL_EDIT",
        "session_protocol",
        f"{session.id}:v{row.version}",
        ip=ip,
    )
    row.updated_at = _now()  # явно: server-side onupdate иначе требует ленивой подгрузки
    await db.flush()
    payload = _payload(row)  # до commit: после него атрибуты истекли (async lazy)
    await db.commit()
    return payload


async def approve(db: AsyncSession, clinician_id, session_id, ip: str | None = None) -> dict:
    """Подтверждение врачом: обязательные разделы §5.2 + хэш канонического JSON."""
    session = await require_session(db, clinician_id, session_id)
    await require_patient_access(db, clinician_id, session.patient_id)
    row = await _active(db, session.id)
    if row is None:
        raise Problem(409, "PROTOCOL_NOT_READY", "Протокол не заполнен")
    if row.status in ("approved", "signed", "submitted"):
        return _payload(row)  # идемпотентно: повторный approve не меняет документ
    sections = decode_sections(row)
    missing = missing_required(sections)
    if missing:
        raise Problem(
            409,
            "PROTOCOL_NOT_READY",
            "Не заполнены обязательные разделы: " + ", ".join(missing),
        )
    row.status = "approved"
    row.approved_by = clinician_id
    row.approved_at = datetime.now(timezone.utc)
    row.content_sha256 = content_sha256(sections)
    await audit.record(
        db,
        clinician_id,
        "SESSION_PROTOCOL_APPROVE",
        "session_protocol",
        f"{session.id}:v{row.version}",
        ip=ip,
    )
    row.updated_at = _now()
    await db.flush()
    payload = _payload(row)
    await db.commit()
    return payload
