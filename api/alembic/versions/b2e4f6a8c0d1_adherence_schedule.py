"""FR-27: режим приёма в назначении и FK отметки приверженности

Revision ID: b2e4f6a8c0d1
Revises: d7e8f9a0b1c2
Create Date: 2026-10-23

Приверженность считается от ожидаемых приёмов, а не от числа отметок:
  * patient_medications.times_per_day — сколько раз в день принимать (1..6).
    NULL у строк импорта ЕМИАС: режим неизвестен, процент по ним не считается.
  * adherence_records.medication_id — FK на назначение (ON DELETE SET NULL).
    Backfill не делается: старые отметки остаются без связи (medication —
    свободный текст) и в знаменатель не входят, это осознанно видно в UI.

Обратимость: downgrade снимает FK/индекс/колонки; текстовые названия и сами
отметки не трогаются, поэтому данные о приёме не теряются.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2e4f6a8c0d1"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("patient_medications", sa.Column("times_per_day", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "patient_medications_times_per_day",
        "patient_medications",
        "times_per_day is null or times_per_day between 1 and 6",
    )
    op.add_column("adherence_records", sa.Column("medication_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "adherence_records_medication_id_fkey",
        "adherence_records",
        "patient_medications",
        ["medication_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_adherence_records_medication_id", "adherence_records", ["medication_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_adherence_records_medication_id", table_name="adherence_records")
    op.drop_constraint(
        "adherence_records_medication_id_fkey", "adherence_records", type_="foreignkey",
    )
    op.drop_column("adherence_records", "medication_id")
    op.drop_constraint(
        "patient_medications_times_per_day", "patient_medications", type_="check",
    )
    op.drop_column("patient_medications", "times_per_day")
