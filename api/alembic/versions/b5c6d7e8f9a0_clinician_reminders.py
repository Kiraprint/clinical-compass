"""clinician reminders (FR-24)

Revision ID: b5c6d7e8f9a0
Revises: e3f4a5b6c7d8
Create Date: 2026-10-22

FR-24: личные напоминания врача по пациенту. note — свободный текст (ФИО/состояние,
спецкатегория ПДн), поэтому bytea + pgp_sym_encrypt (api/app/crypto.EncryptedText),
как у остальных free-text ПДн-колонок. notifications получает reminder_id —
уникальный ключ дедупликации доставки (один in-app Notification на напоминание)
и расширенный CHECK kind ('REMINDER'). clinician_reminders.patient_id — FK users.id:
при отзыве согласия строки уничтожаются в routers/consent._destroy_patient_pdn
(152-ФЗ ст.9 ч.2-3).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b5c6d7e8f9a0"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.create_table(
        "clinician_reminders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("clinician_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("note", sa.LargeBinary(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind in ('call','write','appointment','custom')", name="clinician_reminders_kind"
        ),
        sa.CheckConstraint(
            "status in ('open','done','cancelled')", name="clinician_reminders_status"
        ),
    )
    op.create_index("ix_clinician_reminders_clinician_id", "clinician_reminders", ["clinician_id"])
    op.create_index("ix_clinician_reminders_patient_id", "clinician_reminders", ["patient_id"])
    op.create_index("ix_clinician_reminders_due_at", "clinician_reminders", ["due_at"])
    op.create_index(
        "clinician_reminders_clinician_due",
        "clinician_reminders",
        ["clinician_id", "status", "due_at"],
    )

    op.add_column("notifications", sa.Column("reminder_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "notifications_reminder_id_fkey",
        "notifications",
        "clinician_reminders",
        ["reminder_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "notifications_reminder_id_key", "notifications", ["reminder_id"], unique=True
    )
    op.drop_constraint("notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "notifications_kind", "notifications", "kind in ('RED_ESCALATION','REMINDER')"
    )

def downgrade() -> None:
    # Уведомления-напоминания удаляются раньше таблицы (FK reminder_id).
    op.execute("DELETE FROM notifications WHERE kind = 'REMINDER'")
    op.drop_constraint("notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "notifications_kind", "notifications", "kind in ('RED_ESCALATION')"
    )
    op.drop_index("notifications_reminder_id_key", table_name="notifications")
    op.drop_constraint("notifications_reminder_id_fkey", "notifications", type_="foreignkey")
    op.drop_column("notifications", "reminder_id")

    op.drop_index("clinician_reminders_clinician_due", table_name="clinician_reminders")
    op.drop_index("ix_clinician_reminders_due_at", table_name="clinician_reminders")
    op.drop_index("ix_clinician_reminders_patient_id", table_name="clinician_reminders")
    op.drop_index("ix_clinician_reminders_clinician_id", table_name="clinician_reminders")
    op.drop_table("clinician_reminders")
