"""Чат-поддержка (FR-10, FR-34). Правила, не LLM; эскалация на человека при RED.

Помощник отвечает автоматически не чаще одного раза в календарные сутки
(Europe/Moscow) на пациента: ответы «на каждое сообщение» превращались в
спам-ленту и мешали врачу читать нить. Остальные сообщения сохраняются без
ответа и видны врачу в карте.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Assessment, ChatMessage

MOSCOW = ZoneInfo("Europe/Moscow")

# Спокойный текст без канцелярита и без обещания мгновенного контакта:
# сообщение сохраняется и попадёт врачу в карту, но это не линия экстренной помощи.
FAQ_REPLY = (
    "Спасибо, что написали. Ваше сообщение сохранено — врач увидит его в вашей карте. "
    "Здесь же можно вести дневник настроения и выполнять ежедневные чек-ины."
)
HOTLINE_REPLY = (
    "Спасибо, что написали. Ваше сообщение сохранено — врач увидит его в вашей карте. "
    "Если станет тяжелее, позвоните по одному из номеров помощи ниже."
)


def moscow_day_start_utc(now: datetime | None = None) -> datetime:
    """Начало текущих календарных суток по Москве (UTC-граница для created_at)."""
    local = (now or datetime.now(timezone.utc)).astimezone(MOSCOW)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def _last_triage(db: AsyncSession, patient_id) -> str | None:
    assessment = await db.scalar(
        select(Assessment).where(Assessment.patient_id == patient_id).order_by(Assessment.computed_at.desc())
    )
    return assessment.triage if assessment else None


async def auto_replied_today(db: AsyncSession, patient_id) -> bool:
    """Был ли уже автоответ помощника за сегодня (по календарю Москвы)."""
    count = await db.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .where(
            ChatMessage.patient_id == patient_id,
            ChatMessage.sender == "SYSTEM",
            ChatMessage.created_at >= moscow_day_start_utc(),
        )
    )
    return bool(count)


async def post_message(db: AsyncSession, patient_id, content: str) -> dict:
    """Сохраняет сообщение пациента; автоответ — максимум один в сутки.

    `reply` в ответе — None, если лимит автоответов на сегодня уже израсходован
    (сообщение всё равно сохранено и доступно врачу).
    """
    patient_msg = ChatMessage(patient_id=patient_id, sender="PATIENT", kind="TEXT", content=content)
    db.add(patient_msg)
    reply: ChatMessage | None = None
    if not await auto_replied_today(db, patient_id):
        red = await _last_triage(db, patient_id) == "RED"
        reply = ChatMessage(
            patient_id=patient_id,
            sender="SYSTEM",
            kind="HOTLINE" if red else "FAQ",
            content=HOTLINE_REPLY if red else FAQ_REPLY,
        )
        db.add(reply)
    await db.commit()
    await db.refresh(patient_msg)
    if reply is not None:
        await db.refresh(reply)
    return {
        "message": {"id": str(patient_msg.id), "sender": "PATIENT", "content": content},
        "reply": None if reply is None else {
            "id": str(reply.id),
            "sender": "SYSTEM",
            "kind": reply.kind,
            "content": reply.content,
            "help_resources": (
                [
                    {"title": "Единый телефон доверия", "phone": "8-800-2000-122"},
                    {"title": "Экстренная служба", "phone": "112"},
                ]
                if reply.kind == "HOTLINE"
                else []
            ),
        },
    }


def to_dict(m: ChatMessage) -> dict:
    return {
        "id": str(m.id),
        "sender": m.sender,
        "kind": m.kind,
        "content": m.content,
        "created_at": m.created_at.isoformat(),
    }

async def list_messages(db: AsyncSession, patient_id, limit: int = 50) -> list[ChatMessage]:
    rows = (await db.scalars(
        select(ChatMessage)
        .where(ChatMessage.patient_id == patient_id)
        .order_by(ChatMessage.no.desc())
        .limit(limit)
    )).all()
    return list(reversed(rows))
