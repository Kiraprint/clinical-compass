"""Журнал аудита (append-only, docs/04 §5)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def record(db: AsyncSession, actor_id, action: str, object_type: str, object_id=None, ip: str | None = None) -> None:
    db.add(AuditLog(actor_id=actor_id, action=action, object_type=object_type, object_id=object_id, ip=ip))