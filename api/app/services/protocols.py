"""FR-39/FR-43: протокол приёма — структура §5, валидация §5.2, версии §3.4, approve §4.4.

Протокол — юридически значимая запись приёма (docs/18 §1.1), а не заметка ИИ:
черновик создаёт и подтверждает врач. ИИ (PR P3) заполняет нарративные разделы,
а `diagnosis`/`treatment` МОЖЕТ предложить только со ссылками на чанки KB
(`sections.sources`, §5.3–5.4, FR-43): элемент без источника не переносится,
спорные помечаются `needs_review`. Статус `approved` ставит исключительно врач.

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

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Problem
from app.models import SessionProtocol

from . import audit
from .sessions import PROTOCOL_ACTIVE, require_patient_access, require_session

# §5.2: потолки разделов. Ключ — имя раздела, значение — символов.
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
# §5.4 (FR-43): обоснования черновика. Потолки — чтобы документ не разросся
# и подпись не считалась по мегабайту цитат.
SOURCES_MAX = 50
SOURCE_QUOTE_CHARS = 2000
SOURCE_FIELDS = ("id", "ref", "section", "title", "url")

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
    return Problem(422, "PROTOCOL_SECTION_TOO_LONG", f"{path}: максимум {limit} символов")


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
        if code is not None and not isinstance(code, str):
            raise Problem(422, "VALIDATION_ERROR", "diagnosis.icd10: ожидалась строка")
        if isinstance(code, str) and code.strip() and not ICD10.match(code.strip()):
            raise Problem(
                422, "PROTOCOL_ICD10_INVALID", "diagnosis.icd10: ожидается код МКБ-10 (F41.1)"
            )

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

    sources = sections.get("sources")
    if sources is not None:
        if not isinstance(sources, list):
            raise Problem(422, "VALIDATION_ERROR", "sources: ожидался массив")
        if len(sources) > SOURCES_MAX:
            raise Problem(
                422, "PROTOCOL_SECTION_TOO_LONG", f"sources: максимум {SOURCES_MAX} записей"
            )
        for i, item in enumerate(sources):
            if not isinstance(item, dict):
                raise Problem(422, "VALIDATION_ERROR", f"sources[{i}]: ожидался объект")
            for field in SOURCE_FIELDS:
                _check_text(item.get(field), f"sources[{i}].{field}", LINE_CHARS)
            _check_text(item.get("quote"), f"sources[{i}].quote", SOURCE_QUOTE_CHARS)
            used = item.get("used_by", [])
            if not isinstance(used, list) or not all(isinstance(name, str) for name in used):
                raise Problem(
                    422, "VALIDATION_ERROR", f"sources[{i}].used_by: ожидался массив строк"
                )



    # FR-43/§5.4: ссылка диагноза и лечения обязана указывать на запись из
    # `sources` этого же документа. Иначе подписанный документ несёт
    # обоснование, которое не поддерживает его содержимое (UI мог слить
    # старый черновик с новой выдачей источников).
    known = {item.get("id") for item in (sources or [])}
    refs: list[tuple[str, object]] = []
    diagnosis = sections.get("diagnosis")
    if isinstance(diagnosis, dict) and diagnosis.get("sources") is not None:
        refs.append(("diagnosis.sources", diagnosis.get("sources")))
    for i, item in enumerate(treatment or []):
        if isinstance(item, dict) and item.get("sources") is not None:
            refs.append((f"treatment[{i}].sources", item.get("sources")))
    for label, value in refs:
        if not isinstance(value, list) or not all(isinstance(ref, str) for ref in value):
            raise Problem(422, "VALIDATION_ERROR", f"{label}: ожидался массив строк")
        missing = [ref for ref in value if ref not in known]
        if missing:
            raise Problem(
                422,
                "PROTOCOL_SOURCE_UNKNOWN",
                f"{label}: источника {missing[0]} нет в sources",
            )


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


async def active(db: AsyncSession, session_id) -> SessionProtocol | None:
    """Активная версия протокола (публичный вход для ИИ-блока §4.3)."""
    return await _active(db, session_id)


async def _next_version(db: AsyncSession, session_id) -> int:
    """Следующий номер версии — максимум по сессии, а не последняя активная."""
    current = await db.scalar(
        select(func.max(SessionProtocol.version)).where(
            SessionProtocol.session_id == session_id
        )
    )
    return (current or 0) + 1


async def _create_draft(
    db: AsyncSession,
    session_id,
    sections: dict,
    code,
    clinician_id,
    source: str = "manual",
):
    """Новая версия draft: номер — `max+1`, гонку писателей разбираем повтором.

    Гонка реальна и между разными путями: PUT врача, черновик ИИ по кнопке,
    автодрафт после расшифровки и тул диалога. Все они идут через эту функцию,
    иначе второй писатель получал `IntegrityError` на `UNIQUE(session_id, version)`.

    Повтор — **внутри SAVEPOINT**: занятый номер не должен откатывать транзакцию
    вызывающего. В чате там уже лежит `flush`-нутая реплика врача и загруженная
    сессия — `db.rollback()` выбросил бы реплику и экспайрил объекты (дальше
    `session.id` → `MissingGreenlet`).
    """
    for _ in range(2):
        row = SessionProtocol(
            session_id=session_id,
            version=await _next_version(db, session_id),
            status="draft",
            source=source,
            sections_cipher=encode_sections(sections),
            diagnosis_code=code,
            created_by=clinician_id,
        )
        try:
            # SAVEPOINT: после отката объект выпадает из сессии, транзакция
            # вызывающего (реплика врача, правки PUT) остаётся целой.
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError:
            # UNIQUE(session_id, version): параллельный писатель забрал номер — берём следующий.
            continue
        # §3.4: активна максимум одна версия. В `superseded` уходят только
        # неотправленные версии: `submitted` уже зарегистрирована в РЭМД (FR-41),
        # и её статус не должен меняться за спиной доставки.
        await db.execute(
            update(SessionProtocol)
            .where(
                SessionProtocol.session_id == session_id,
                SessionProtocol.id != row.id,
                SessionProtocol.status.in_(("draft", "approved", "signed")),
            )
            .values(status="superseded", updated_at=_now())
        )
        await db.flush()
        return row
    raise Problem(409, "PROTOCOL_LOCKED", "Протокол изменяется параллельно — повторите")


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


def _resolve_code(code: str | None, sections: dict) -> str | None:
    """`diagnosis_code` и `sections.diagnosis.icd10` — один и тот же диагноз (§3.4/§5.1)."""
    raw = (sections.get("diagnosis") or {}).get("icd10")
    section_code = raw.strip() if isinstance(raw, str) and raw.strip() else None
    if code and section_code and code != section_code:
        raise Problem(
            422,
            "PROTOCOL_ICD10_CONFLICT",
            "diagnosis_code не совпадает с sections.diagnosis.icd10",
        )
    return code or section_code


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
    # `sid` вместо `session.id`: после rollback внутри гонки версий атрибуты истекли.
    sid = session.id
    validate_sections(sections)
    code = _resolve_code(_check_code(diagnosis_code), sections)

    active = await _active(db, sid)
    if active is None:
        # Номера идемпотентны: после `rejected`/`superseded` история есть, а активной нет.
        row = await _create_draft(db, sid, sections, code, clinician_id)
    elif active.status == "approved":
        raise Problem(409, "PROTOCOL_LOCKED", "Протокол подтверждён — правка запрещена")
    elif active.status == "submitted":
        raise Problem(409, "PROTOCOL_LOCKED", "Протокол передан в ЕМИАС — правка запрещена")
    elif active.status == "signed":
        # Подпись не переносится на новые байты: новая версия + повторная подпись.
        row = await _create_draft(db, sid, sections, code, clinician_id)
    else:  # draft
        active.sections_cipher = encode_sections(sections)
        # Присваиваем всегда: код, удалённый из разделов, не должен остаться в строке.
        active.diagnosis_code = code
        active.content_sha256 = None  # approve пересчитает хэш новых байтов
        row = active

    # Аудит хранит только ids (`action`, `object_type`, `object_id`) — текст
    # разделов в журнал не попадает (docs/18 §2).
    await audit.record(
        db,
        clinician_id,
        "SESSION_PROTOCOL_EDIT",
        "session_protocol",
        f"{sid}:v{row.version}",
        ip=ip,
    )
    row.updated_at = _now()  # явно: server-side onupdate иначе требует ленивой подгрузки
    await db.flush()
    payload = _payload(row)  # до commit: после него атрибуты истекли (async lazy)
    await db.commit()
    return payload


async def save_llm_draft(
    db: AsyncSession,
    clinician_id,
    session_id,
    sections: dict,
    model: str | None = None,
    prompt_version: str | None = None,
    needs_review: list[dict] | None = None,
    ip: str | None = None,
    action: str = "SESSION_PROTOCOL_DRAFT",
) -> dict:
    """Черновик ИИ (§4.3): версия `source='llm'`, сам никогда не подтверждается.

    Активный draft правится на месте (как PUT §4.4) — повторный черновик не
    плодит версии, а заменяет предыдущий. Подтверждённая/подписанная/переданная
    запись не перезаписывается: 409 PROTOCOL_LOCKED (ИИ не трогает документ
    после врача, §1.1). `needs_review` — подсказки врачу, не блокировка.

    `action` (FR-44): какой код писать в журнал. `audit.record` не принимает
    metadata, поэтому единственный честный способ отличить ручной черновик от
    автодрафта конвейера и от вызова тулом диалога — код действия:
    `SESSION_PROTOCOL_DRAFT` (кнопка/POST), `SESSION_PROTOCOL_AUTODRAFT`
    (после расшифровки), `SESSION_PROTOCOL_CHAT_DRAFT` (тул агента).
    """
    session = await require_session(db, clinician_id, session_id)
    # id — локально: повтор версии внутри `_create_draft` делает rollback, после
    # которого обращение к `session.id` потребовало бы ленивой подгрузки.
    sid = session.id
    await require_patient_access(db, clinician_id, session.patient_id)
    active = await _active(db, sid)
    if active is not None and active.status != "draft":
        raise Problem(409, "PROTOCOL_LOCKED", "Протокол подтверждён — черновик недоступен")

    validate_sections(sections)
    if active is None:
        # Тот же путь, что у PUT: гонка версий (автодрафт/кнопка/тул/врач)
        # разбирается повтором, а не 500 из `UNIQUE(session_id, version)`.
        row = await _create_draft(db, sid, sections, None, clinician_id, source="llm")
    else:
        row = active
        row.sections_cipher = encode_sections(sections)
        row.source = "llm"
        row.content_sha256 = None  # approve пересчитает хэш новых байтов

    row.model = (model or "")[:64] or None
    row.prompt_version = (prompt_version or "")[:16] or None
    row.needs_review = list(needs_review or [])
    row.updated_at = _now()
    # Аудит — только ids (docs/18 §2); разделы и текст расшифровки не пишутся.
    await audit.record(
        db,
        clinician_id,
        action,
        "session_protocol",
        f"{sid}:v{row.version}",
        ip=ip,
    )
    await db.flush()
    payload = _payload(row)
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
    # `diagnosis_code` досчитывается из разделов: подтверждённая запись не может
    # расходиться с `sections.diagnosis.icd10` (расхождение — 422).
    row.diagnosis_code = _resolve_code(row.diagnosis_code, sections)
    row.status = "approved"
    row.approved_by = clinician_id
    row.approved_at = _now()
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
