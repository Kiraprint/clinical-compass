"""Чат-поддержка (FR-10). Правила, не LLM; эскалация на человека при RED."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Assessment, ChatMessage

FAQ_REPLY = "Спасибо, что поделились. Если состояние резко ухудшается, обратитесь к вашему врачу. Здесь вы можете вести дневник настроения и выполнять ежедневные чек-ины."

HOTLINE_REPLY = "Мы заметили, что вам сейчас тяжело. Важно, чтобы вы не оставались с этим наедине: свяжитесь с вашим врачом или позвоните по одному из номеров помощи прямо сейчас."


async def _last_triage(db: AsyncSession, patient_id) -> str | None:
    assessment = await db.scalar(
        select(Assessment).where(Assessment.patient_id == patient_id).order_by(Assessment.computed_at.desc())
    )
    return assessment.triage if assessment else None


async def post_message(db: AsyncSession, patient_id, content: str) -> dict:
    patient_msg = ChatMessage(patient_id=patient_id, sender="PATIENT", kind="TEXT", content=content)
    db.add(patient_msg)
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
    await db.refresh(reply)
    return {
        "message": {"id": str(patient_msg.id), "sender": "PATIENT", "content": content},
        "reply": {
            "id": str(reply.id),
            "sender": "SYSTEM",
            "kind": reply.kind,
            "content": reply.content,
            "help_resources": (
                [
                    {"title": "Единый телефон доверия", "phone": "8-800-2000-122"},
                    {"title": "Экстренная служба", "phone": "112"},
                ]
                if red
                else []
            ),
        },
    }


async def list_messages(db: AsyncSession, patient_id, limit: int = 50) -> list[ChatMessage]:
    rows = (await db.scalars(
        select(ChatMessage)
        .where(ChatMessage.patient_id == patient_id)
        .order_by(ChatMessage.no.desc())
        .limit(limit)
    )).all()
    return list(reversed(rows))


def to_dict(msg: ChatMessage) -> dict:
    return {
        "id": str(msg.id),
        "sender": msg.sender,
        "kind": msg.kind,
        "content": msg.content,
        "created_at": msg.created_at.isoformat(),
    }