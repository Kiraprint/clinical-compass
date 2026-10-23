"""FR-31: факты из переписки врача с пациентом — черновики с решением врача.

Врач сам запускает анализ (`POST .../chat/analyze`): LLM получает только
деперсонализированную переписку (ФИО пациента/врача и структурные ПДн
маскируются scrub_for_llm, docs/12) и возвращает до 10 фактов — исключительно
явные утверждения пациента. Факты сохраняются как `pending`; в карточку
попадают только `accepted` (врач подтвердил), `rejected` не показываются.
Аудит (CHAT_INSIGHTS_RUN/DECIDE) пишет только счётчики, без текстов.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import Problem
from app.models import ChatInsight, ChatMessage, User

from . import llm_router
from .agent import scrub_for_llm
from .audit import record as audit_record
from .clinician import require_patient_consent, require_roster, roster_names

logger = logging.getLogger(__name__)

KINDS = ("symptom", "medication", "adherence", "sleep", "substance", "risk", "other")

WINDOW_DAYS = 30       # окно переписки для анализа
MAX_MESSAGES = 100     # последних сообщений
MESSAGE_CHARS = 500    # обрезка одного сообщения
DIALOGUE_CHARS = 6000  # обрезка всей переписки (с конца: свежее важнее)
MAX_FACTS = 10         # потолок фактов за один запуск
FACT_CHARS = 200       # потолок текста факта

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
_WS = re.compile(r"\s+")

_SYSTEM_PROMPT = (
    "Ты извлекаешь факты из переписки врача и пациента. Используй ТОЛЬКО явные "
    "утверждения самого пациента о его самочувствии, препаратах, приверженности "
    "лечению, сне, употреблении психоактивных веществ и суицидальном риске. "
    "Не додумывай, не интерпретируй намёки, не ставь диагнозы и не давай советов. "
    "Если явных утверждений нет — верни пустой массив [].\n"
    "Извлеки не более 10 фактов. "
    "Ответ — строго JSON-массив объектов вида "
    '{"kind": "...", "text": "...", "confidence": 0.0}, без пояснений и markdown. '
    "kind: symptom (симптом/жалоба) | medication (приём/доза/эффект препарата) | "
    "adherence (пропуски/регулярность приёма) | sleep (сон) | substance (алкоголь, "
    "никотин, другие ПАВ) | risk (суицидальные мысли/самоповреждение) | other. "
    f"text — короткая формулировка от третьего лица, до {FACT_CHARS} символов. "
    "confidence — твоя уверенность от 0 до 1."
)

_DISABLED_DETAIL = (
    "Анализ переписки выключен: нужна локальная модель (CC_ASSISTANT_LLM_URL, "
    "docs/17) и CC_CHAT_INSIGHTS_ENABLED=true"
)


def enabled() -> bool:
    """Фича работает только при явном флаге И настроенном OpenAI-совместимом /v1."""
    return bool(settings.chat_insights_enabled and settings.assistant_llm_url.strip())


def _require_enabled() -> None:
    if not enabled():
        raise Problem(503, "CHAT_INSIGHTS_DISABLED", _DISABLED_DETAIL)


def _insight_dict(i: ChatInsight) -> dict:
    return {
        "id": str(i.id),
        "patient_id": str(i.patient_id),
        "kind": i.kind,
        "text": i.text,
        "confidence": i.confidence,
        "source_message_ids": list(i.source_message_ids or []),
        "status": i.status,
        "created_at": i.created_at.isoformat(),
        "reviewed_at": i.reviewed_at.isoformat() if i.reviewed_at else None,
    }


async def _recent_messages(db: AsyncSession, patient_id) -> list[ChatMessage]:
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    rows = (await db.scalars(
        select(ChatMessage)
        .where(ChatMessage.patient_id == patient_id, ChatMessage.created_at >= since)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.no.desc())
        .limit(MAX_MESSAGES)
    )).all()
    return list(reversed(rows))


def _dialogue_text(messages: list[ChatMessage]) -> str:
    """Хронологическая переписка: каждое сообщение ≤500, вся — ≤6000 символов.

    Переполнение отрезает самые старые строки: свежая часть диалога важнее.
    """
    labels = {"PATIENT": "Пациент", "CLINICIAN": "Врач", "SYSTEM": "Помощник"}
    lines = [
        f"{labels.get(m.sender, 'Пациент')}: {m.content.strip()[:MESSAGE_CHARS]}"
        for m in messages
    ]
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        add = len(line) + (1 if kept else 0)
        if total + add > DIALOGUE_CHARS:
            break
        kept.append(line)
        total += add
    return "\n".join(reversed(kept))


async def _deny_names(db: AsyncSession, clinician_id, patient_id) -> list[str]:
    """ФИО пациента и врача + ростер врача — deny-list деперсонализации."""
    names = await roster_names(db, clinician_id)
    for uid in (patient_id, clinician_id):
        fio = await db.scalar(select(User.fio).where(User.id == uid))
        if fio and fio.strip() and fio not in names:
            names.append(fio)
    return names


def _normalize(text: str) -> str:
    text = _WS.sub(" ", text.strip().lower())
    return re.sub(r"[.!?;,:]+$", "", text)


def _dedupe_hash(patient_id, kind: str, text: str) -> str:
    raw = f"{patient_id}|{kind}|{_normalize(text)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_facts(content: str) -> list[dict]:
    """Устойчиво разбирает ответ модели: markdown-обёртки, лишний текст, мусор."""
    fences = _FENCE.findall(content)
    payload = fences[0] if fences else content
    start, end = payload.find("["), payload.rfind("]")
    if start == -1 or end <= start:
        raise Problem(503, "CHAT_INSIGHTS_UNAVAILABLE", "Модель вернула не JSON-массив")
    try:
        data = json.loads(payload[start : end + 1])
    except (ValueError, TypeError):
        raise Problem(503, "CHAT_INSIGHTS_UNAVAILABLE", "Модель вернула невалидный JSON")
    if not isinstance(data, list):
        raise Problem(503, "CHAT_INSIGHTS_UNAVAILABLE", "Модель вернула не JSON-массив")

    facts: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        raw_text = item.get("text")
        if kind not in KINDS or not isinstance(raw_text, str):
            continue
        text = _WS.sub(" ", raw_text).strip()[:FACT_CHARS].rstrip()
        if not text:
            continue
        confidence = item.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence = None
        elif not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            confidence = None
        else:
            confidence = round(float(confidence), 3)
        facts.append({"kind": kind, "text": text, "confidence": confidence})
        if len(facts) >= MAX_FACTS:
            break
    return facts


def _ask_llm(dialogue: str) -> list[dict]:
    """Один вызов chat/completions. Любая ошибка → 503, в БД ничего не пишем."""
    try:
        resp = llm_router.chat_completion([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Переписка:\n{dialogue}\n\nВерни JSON-массив фактов."},
        ])
        content = resp["choices"][0]["message"].get("content") or ""
    except Problem:
        raise
    except Exception as exc:  # таймаут, сеть, неожиданный формат ответа
        logger.warning("chat_insights: LLM unavailable (%s)", type(exc).__name__)
        raise Problem(
            503, "CHAT_INSIGHTS_UNAVAILABLE", "Модель недоступна — факты не сохранены"
        ) from exc
    return _parse_facts(content)


async def analyze(db: AsyncSession, clinician_id, patient_id, ip: str | None = None) -> dict:
    """Запуск анализа переписки врачом. Roster + consent + enabled-гейты."""
    _require_enabled()
    await require_roster(db, clinician_id, patient_id)
    await require_patient_consent(db, patient_id)

    messages = await _recent_messages(db, patient_id)
    if not messages:
        raise Problem(409, "CHAT_INSIGHTS_EMPTY", "Переписки с пациентом нет — анализировать нечего")

    dialogue = _dialogue_text(messages)
    # Сырое ФИО в LLM не уходит: маскируем пациента, врача и остальной ростер.
    names = await _deny_names(db, clinician_id, patient_id)
    # Деперсонализация (regexp по всей переписке) и HTTP к LLM — блокирующие:
    # в event loop FastAPI их пускать нельзя.
    dialogue, _, _ = await anyio.to_thread.run_sync(
        lambda: scrub_for_llm(dialogue, None, names=names)
    )
    facts = await anyio.to_thread.run_sync(_ask_llm, dialogue)
    source_ids = [str(m.id) for m in messages]
    existing = set(
        (await db.scalars(
            select(ChatInsight.dedupe_hash).where(ChatInsight.patient_id == patient_id)
        )).all()
    )

    created: list[ChatInsight] = []
    skipped = 0
    for fact in facts:
        digest = _dedupe_hash(patient_id, fact["kind"], fact["text"])
        if digest in existing:
            skipped += 1
            continue
        existing.add(digest)
        row = ChatInsight(
            patient_id=patient_id,
            kind=fact["kind"],
            text=fact["text"],
            confidence=fact["confidence"],
            source_message_ids=source_ids,
            status="pending",
            dedupe_hash=digest,
            created_by=clinician_id,
        )
        db.add(row)
        created.append(row)

    await db.flush()
    await audit_record(db, clinician_id, "CHAT_INSIGHTS_RUN", "patient", str(patient_id), ip=ip)
    await db.commit()
    return {
        "created": len(created),
        "skipped": skipped,
        "items": [_insight_dict(i) for i in created],
    }


async def list_insights(
    db: AsyncSession, clinician_id, patient_id, status: str | None = None
) -> list[dict]:
    """Черновики/решения по пациенту из ростра врача (фича должна быть включена)."""
    _require_enabled()
    await require_roster(db, clinician_id, patient_id)
    query = select(ChatInsight).where(ChatInsight.patient_id == patient_id)
    if status:
        query = query.where(ChatInsight.status == status)
    rows = (await db.scalars(query.order_by(ChatInsight.created_at.desc()))).all()
    return [_insight_dict(i) for i in rows]


async def decide(
    db: AsyncSession, clinician_id, patient_id, insight_id, status: str, ip: str | None = None
) -> dict:
    """Решение врача по факту. Чужой/несуществующий → 404; повтор идемпотентен."""
    await require_roster(db, clinician_id, patient_id)
    row = await db.scalar(
        select(ChatInsight).where(
            ChatInsight.id == insight_id, ChatInsight.patient_id == patient_id
        )
    )
    if row is None:
        raise Problem(404, "INSIGHT_NOT_FOUND", "Факт не найден")
    if row.status != status:
        row.status = status
        row.reviewed_by = clinician_id
        row.reviewed_at = datetime.now(timezone.utc)
        await audit_record(db, clinician_id, "CHAT_INSIGHTS_DECIDE", "patient", str(patient_id), ip=ip)
        await db.commit()
    return _insight_dict(row)


async def accepted_for_report(db: AsyncSession, patient_id) -> list[dict]:
    """Подтверждённые врачом факты для карточки (без source_message_ids)."""
    rows = (await db.scalars(
        select(ChatInsight)
        .where(ChatInsight.patient_id == patient_id, ChatInsight.status == "accepted")
        .order_by(ChatInsight.created_at.desc())
    )).all()
    return [
        {
            "id": str(i.id),
            "kind": i.kind,
            "text": i.text,
            "created_at": i.created_at.isoformat(),
            "reviewed_at": i.reviewed_at.isoformat() if i.reviewed_at else None,
        }
        for i in rows
    ]
