"""FR-37: анамнез пациента — вводные врача и черновик структурированного текста.

Врач ведёт «краткие вводные» (`brief`) и формирует из них черновик анамнеза
(разделы: Жалобы, Анамнез заболевания, Анамнез жизни, Текущий статус). Черновик
модели НЕ сохраняется: врач подтверждает его через PUT, и только тогда текст
попадает в `patient_anamnesis.text`.

Границы (docs/12, docs/10): перед вызовом LLM текст проходит
`assistant_chat.scrub_for_llm` (структурные ПДн + ML + deny-list ростера); в
CC_PROD_MODE при недоступном ML-backend поднимается 503
DEPERSONALIZATION_UNAVAILABLE и сырой текст в LLM не уходит, вне prod —
regex-деградация с WARNING. В логи и аудит попадают только ids и категории
масок, без текста.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import Problem
from app.models import Assessment, CheckinSession, PatientAnamnesis

from . import llm_router
from .agent import scrub_for_llm
from .audit import record as audit_record
from .clinician import require_patient_consent, require_roster
from .depersonalize import deny_names

logger = logging.getLogger(__name__)

BRIEF_CHARS = 4000     # потолок «кратких вводных» (контракт PUT)
TEXT_CHARS = 20000     # потолок подтверждённого анамнеза (не даём раздуть карточку)
MAX_CHECKINS = 5       # последние чек-ины в контекст, если brief пуст
MAX_ASSESSMENTS = 3    # последние скрининги в контекст

# Структура, которую обязана вернуть модель. Порядок — клинический: от жалоб
# к текущему статусу. Промпт и тест держат один список.
SECTIONS = ("Жалобы", "Анамнез заболевания", "Анамнез жизни", "Текущий статус")

_FENCE = re.compile(r"```(?:markdown|md)?\s*(.*?)```", re.S | re.I)
_BLANKS = re.compile(r"\n{3,}")

_SYSTEM_PROMPT = (
    "Ты помогаешь врачу оформить анамнез пациента. Используй ТОЛЬКО переданные "
    "сведения, ничего не додумывай и не добавляй фактов, которых нет во входных "
    "данных. Не ставь диагноз, не назначай лечение и не давай рекомендаций. "
    "Пиши по-русски, кратко и клинически нейтрально. "
    "Верни ровно четыре раздела с заголовками и списками внутри:\n"
    "Жалобы\nАнамнез заболевания\nАнамнез жизни\nТекущий статус\n"
    "Если для раздела данных нет, напиши «нет данных». "
    "Не используй персональные данные: в тексте уже стоят маски вида [ФИО], "
    "[ТЕЛЕФОН], [АДРЕС] — сохраняй их как есть."
)

_USER_TEMPLATE = (
    "Сведения для анамнеза:\n{source}\n\n"
    "Сформируй структурированный анамнез по четырём разделам."
)


def _ensure_enabled() -> None:
    if not settings.anamnesis_enabled:
        raise Problem(
            503,
            "ANAMNESIS_DISABLED",
            "Анамнез выключен: включите CC_ANAMNESIS_ENABLED=true",
        )


def _ensure_llm() -> None:
    if not settings.assistant_llm_url.strip():
        raise Problem(
            503,
            "ANAMNESIS_UNAVAILABLE",
            "Модель не настроена (CC_ASSISTANT_LLM_URL) — черновик недоступен",
        )


def _payload(row: PatientAnamnesis | None, patient_id) -> dict:
    """Ответ API: пустое состояние — те же ключи с null (UI не различает 404)."""
    if row is None:
        return {
            "patient_id": str(patient_id),
            "brief": None,
            "text": None,
            "updated_at": None,
            "updated_by": None,
        }
    return {
        "patient_id": str(row.patient_id),
        "brief": row.brief,
        "text": row.text,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": str(row.updated_by) if row.updated_by else None,
    }


async def _row(db: AsyncSession, patient_id) -> PatientAnamnesis | None:
    return await db.scalar(
        select(PatientAnamnesis).where(PatientAnamnesis.patient_id == patient_id)
    )


def payload_of(row: PatientAnamnesis | None, patient_id) -> dict:
    """Анамнез по уже загруженной строке: карточка врача читает её первым запросом."""
    return _payload(row, patient_id)


async def get(db: AsyncSession, clinician_id, patient_id) -> dict:
    """Чтение анамнеза: roster + активное согласие пациента."""
    # Гейты доступа первыми: чужой пациент получает тот же 403 NO_ROSTER, что и
    # остальные разделы карточки, и не зависит от состояния флага.
    await require_roster(db, clinician_id, patient_id)
    await require_patient_consent(db, patient_id)
    _ensure_enabled()
    return _payload(await _row(db, patient_id), patient_id)


async def save(
    db: AsyncSession,
    clinician_id,
    patient_id,
    brief: str,
    text: str | None = None,
    ip: str | None = None,
) -> dict:
    """PUT: upsert вводных; `text` (подтверждённый черновик) — только если передан.

    Roster + активное согласие. Аудит ANAMNESIS_WRITE — только ids, без текста.
    """
    await require_roster(db, clinician_id, patient_id)
    await require_patient_consent(db, patient_id)
    _ensure_enabled()
    if len(brief) > BRIEF_CHARS:
        raise Problem(422, "VALIDATION_ERROR", f"Краткие вводные: максимум {BRIEF_CHARS} символов")
    if text is not None and len(text) > TEXT_CHARS:
        raise Problem(422, "VALIDATION_ERROR", f"Анамнез: максимум {TEXT_CHARS} символов")

    cleaned_brief = brief.strip() or None
    now = datetime.now(timezone.utc)
    row = await _row(db, patient_id)
    if row is None:
        row = PatientAnamnesis(
            patient_id=patient_id,
            brief=cleaned_brief,
            text=text,
            created_by=clinician_id,
            updated_by=clinician_id,
        )
        db.add(row)
    else:
        row.brief = cleaned_brief
        if text is not None:
            row.text = text
        row.updated_by = clinician_id
        row.updated_at = now
    await db.flush()
    await audit_record(db, clinician_id, "ANAMNESIS_WRITE", "patient", str(patient_id), ip=ip)
    await db.commit()
    return _payload(row, patient_id)


async def draft(
    db: AsyncSession,
    clinician_id,
    patient_id,
    brief: str | None = None,
    ip: str | None = None,
) -> dict:
    """POST draft: LLM формирует черновик, ничего не сохраняя.

    Источник — переданные вводные, а при их отсутствии последние чек-ины и
    скрининги. Текст деперсонализируется ДО LLM; ответ содержит только черновик
    и счётчики масок. Аудит ANAMNESIS_DRAFT — ids и категории, без текста.
    """
    await require_roster(db, clinician_id, patient_id)
    await require_patient_consent(db, patient_id)
    _ensure_enabled()
    _ensure_llm()

    source = (brief or "").strip()
    if not source:
        source = await _history_context(db, patient_id)
    if not source:
        raise Problem(
            409,
            "ANAMNESIS_EMPTY",
            "Нет ни вводных, ни данных наблюдения — черновик не из чего строить",
        )

    # Сырое ФИО/телефон в LLM не уходит: deny-list ростера + сам пациент и врач.
    names = await deny_names(db, clinician_id, patient_id)
    # Деперсонализация блокирующая (regex+NER) — не в event loop FastAPI.
    masked, _, report = await anyio.to_thread.run_sync(
        lambda: scrub_for_llm(source, None, names)
    )
    if not masked.strip():
        raise Problem(409, "ANAMNESIS_EMPTY", "После маскирования не осталось текста")
    text = await anyio.to_thread.run_sync(_ask_llm, masked)
    await audit_record(db, clinician_id, "ANAMNESIS_DRAFT", "patient", str(patient_id), ip=ip)
    await db.commit()
    counts = report.get("masked") or {}
    return {"draft": text, "masked_report": {"applied": bool(counts), "counts": counts}}


async def _history_context(db: AsyncSession, patient_id) -> str:
    """Структурированные данные наблюдения без свободного текста.

    Числовые баллы, тяжесть, триаж и ответы чек-ина — медицинские данные, но не
    прямые идентификаторы; всё равно проходят scrub_for_llm перед LLM.
    """
    checkins = (await db.scalars(
        select(CheckinSession)
        .where(CheckinSession.patient_id == patient_id)
        .order_by(CheckinSession.checkin_date.desc())
        .limit(MAX_CHECKINS)
    )).all()
    assessments = (await db.scalars(
        select(Assessment)
        .where(Assessment.patient_id == patient_id)
        .order_by(Assessment.computed_at.desc())
        .limit(MAX_ASSESSMENTS)
    )).all()

    lines: list[str] = []
    for a in reversed(assessments):
        lines.append(
            f"Скрининг {a.computed_at.date().isoformat()}: балл {a.total_score}, "
            f"тяжесть {a.severity}, триаж {a.triage}"
        )
    for c in reversed(checkins):
        answers = ", ".join(f"{k}={v}" for k, v in sorted(c.answers.items()))
        lines.append(
            f"Чек-ин {c.checkin_date.isoformat()}: статус {c.status}, "
            f"риск {c.safety}" + (f", ответы {answers}" if answers else "")
        )
    return "\n".join(lines)


def _is_timeout(exc: BaseException) -> bool:
    """Таймаут urllib/socket (в т.ч. завёрнутый в URLError)."""
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(getattr(exc, "reason", None), TimeoutError)


def _clean(content: str) -> str:
    """Снимает markdown-обёртку и лишние пустые строки; текст не переписывает."""
    match = _FENCE.search(content or "")
    text = (match.group(1) if match else content or "").strip()
    return _BLANKS.sub("\n\n", text)


def _ask_llm(source: str) -> str:
    """Один вызов chat/completions + один ретрай на таймаут → 503.

    В лог — только тип ошибки и длительность; текст вводных не логируется.
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _USER_TEMPLATE.format(source=source)},
    ]
    timeout = settings.anamnesis_timeout
    content = ""
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            resp = llm_router.chat_completion(messages, timeout=timeout)
            content = resp["choices"][0]["message"].get("content") or ""
            break
        except Problem:
            raise
        except Exception as exc:
            elapsed = time.monotonic() - started
            if attempt == 1 and _is_timeout(exc):
                logger.warning(
                    "anamnesis: LLM timeout after %.1fs (timeout=%.0fs), retry",
                    elapsed, timeout,
                )
                continue
            logger.warning(
                "anamnesis: LLM unavailable after %.1fs (%s)", elapsed, type(exc).__name__
            )
            raise Problem(
                503, "ANAMNESIS_UNAVAILABLE", "Модель недоступна — черновик не сформирован"
            ) from exc
    cleaned = _clean(content)
    if not cleaned:
        raise Problem(503, "ANAMNESIS_UNAVAILABLE", "Модель вернула пустой ответ")
    return cleaned
