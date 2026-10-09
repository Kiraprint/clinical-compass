"""Журнал аудита (append-only, docs/04 §5)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditLog


def record(db: Session, actor_id, action: str, object_type: str, object_id=None, ip: str | None = None) -> None:
    db.add(AuditLog(actor_id=actor_id, action=action, object_type=object_type, object_id=object_id, ip=ip))