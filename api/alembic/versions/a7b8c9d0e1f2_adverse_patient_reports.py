"""FR-28: НЯ от пациента и проверка врачом + связь со справочником ГРЛС

Revision ID: a7b8c9d0e1f2
Revises: b2e4f6a8c0d1
Create Date: 2026-10-23

adverse_events:
  * reported_by ('clinician'|'patient') — кто сообщил; server_default clinician.
  * status ('pending'|'confirmed'|'rejected') — состояние проверки;
    server_default confirmed: все исторические строки внёс врач и они валидны.
  * clinician_id становится nullable — у сообщения пациента автора-врача нет.
  * drug_id → drugs.drug_id (справочник ГРЛС), medication_id → patient_medications.id
    (ON DELETE SET NULL), verified_by/verified_at/verification_note — результат
    проверки. verification_note — спецкатегория ПДн, хранится EncryptedText (bytea).
  * индекс (status, created_at desc) — очередь проверки.

notifications.kind: добавляется ADVERSE_EVENT (серьёзное НЯ от пациента).

Обратимость: downgrade удаляет только то, что старая модель не умеет хранить —
сообщения пациентов без врача (pending/rejected), а подтверждённые пациентские
строки сохраняются с clinician_id=verified_by; уведомления ADVERSE_EVENT удаляются
перед восстановлением старого CHECK. Врачебные НЯ не теряются.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "b2e4f6a8c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "adverse_events",
        sa.Column("reported_by", sa.String(16), nullable=False, server_default=sa.text("'clinician'")),
    )
    op.add_column(
        "adverse_events",
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'confirmed'")),
    )
    op.alter_column("adverse_events", "clinician_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("adverse_events", sa.Column("drug_id", sa.String(64), nullable=True))
    op.add_column("adverse_events", sa.Column("medication_id", sa.Uuid(), nullable=True))
    op.add_column("adverse_events", sa.Column("verified_by", sa.Uuid(), nullable=True))
    op.add_column(
        "adverse_events",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # pgcrypto: спецкатегория ПДн, в БД — bytea.
    op.add_column(
        "adverse_events",
        sa.Column("verification_note", sa.dialects.postgresql.BYTEA(), nullable=True),
    )

    op.create_foreign_key(
        "adverse_events_drug_id_fkey", "adverse_events", "drugs", ["drug_id"], ["drug_id"],
    )
    op.create_foreign_key(
        "adverse_events_medication_id_fkey", "adverse_events", "patient_medications",
        ["medication_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "adverse_events_verified_by_fkey", "adverse_events", "users", ["verified_by"], ["id"],
    )
    op.create_index("ix_adverse_events_drug_id", "adverse_events", ["drug_id"])
    op.create_index("ix_adverse_events_medication_id", "adverse_events", ["medication_id"])
    op.create_index(
        "ix_adverse_events_status_created",
        "adverse_events",
        ["status", sa.text("created_at desc")],
    )
    op.create_check_constraint(
        "adverse_events_reported_by", "adverse_events", "reported_by in ('clinician','patient')",
    )
    op.create_check_constraint(
        "adverse_events_status", "adverse_events", "status in ('pending','confirmed','rejected')",
    )

    op.drop_constraint("notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "notifications_kind", "notifications",
        "kind in ('RED_ESCALATION','REMINDER','ADVERSE_EVENT')",
    )


def downgrade() -> None:
    op.drop_constraint("notifications_kind", "notifications", type_="check")
    op.execute("DELETE FROM notifications WHERE kind = 'ADVERSE_EVENT'")
    op.create_check_constraint(
        "notifications_kind", "notifications", "kind in ('RED_ESCALATION','REMINDER')",
    )

    op.drop_constraint("adverse_events_status", "adverse_events", type_="check")
    op.drop_constraint("adverse_events_reported_by", "adverse_events", type_="check")
    op.drop_index("ix_adverse_events_status_created", table_name="adverse_events")
    op.drop_index("ix_adverse_events_medication_id", table_name="adverse_events")
    op.drop_index("ix_adverse_events_drug_id", table_name="adverse_events")

    # Старая модель: у НЯ всегда есть врач-автор. Подтверждённые пациентские строки
    # сохраняем с автором-верификатором, непроверенные/отклонённые удаляем.
    op.execute(
        "UPDATE adverse_events SET clinician_id = verified_by "
        "WHERE clinician_id IS NULL AND verified_by IS NOT NULL"
    )
    op.execute("DELETE FROM adverse_events WHERE clinician_id IS NULL")

    op.drop_constraint("adverse_events_verified_by_fkey", "adverse_events", type_="foreignkey")
    op.drop_constraint("adverse_events_medication_id_fkey", "adverse_events", type_="foreignkey")
    op.drop_constraint("adverse_events_drug_id_fkey", "adverse_events", type_="foreignkey")
    op.alter_column("adverse_events", "clinician_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("adverse_events", "verification_note")
    op.drop_column("adverse_events", "verified_at")
    op.drop_column("adverse_events", "verified_by")
    op.drop_column("adverse_events", "medication_id")
    op.drop_column("adverse_events", "drug_id")
    op.drop_column("adverse_events", "status")
    op.drop_column("adverse_events", "reported_by")
