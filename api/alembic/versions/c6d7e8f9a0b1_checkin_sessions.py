"""checkin_sessions: ежедневный адаптивный чек-ин (docs/16 §5).

Ответы и presented хранятся как bytea (EncryptedText: pgp_sym_encrypt) — спецкатегория
ПДн в покое зашифрована. Уникальность дня перенесена с частичного индекса runs на
(patient_id, checkin_date) в новую таблицу. Эскалации теперь могут ссылаться на
чек-ин вместо assessment.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-10-23
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "c6d7e8f9a0b1"
down_revision: Union[str, None] = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "checkin_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("checkin_date", sa.Date(), nullable=False),
        sa.Column("rules_version", sa.String(length=16), nullable=False),
        sa.Column("answers", sa.LargeBinary(), nullable=False),
        sa.Column("presented", sa.LargeBinary(), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("safety", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["patient_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("patient_id", "checkin_date", name="checkin_sessions_daily"),
    )
    op.create_index(
        op.f("ix_checkin_sessions_patient_id"), "checkin_sessions", ["patient_id"], unique=False
    )

    op.alter_column("escalations", "assessment_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("escalations", sa.Column("checkin_session_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "escalations_checkin_session_id_fkey",
        "escalations",
        "checkin_sessions",
        ["checkin_session_id"],
        ["id"],
    )

    op.drop_index(
        "runs_checkin_daily",
        table_name="runs",
        postgresql_where=sa.text("mode = 'CHECKIN'"),
    )


def downgrade() -> None:
    op.create_index(
        "runs_checkin_daily",
        "runs",
        ["patient_id", sa.literal_column("((started_at AT TIME ZONE 'Europe/Moscow')::date)")],
        unique=True,
        postgresql_where=sa.text("mode = 'CHECKIN'"),
    )

    op.drop_constraint("escalations_checkin_session_id_fkey", "escalations", type_="foreignkey")
    op.drop_column("escalations", "checkin_session_id")
    # NOT NULL восстанавливаем только если данных без assessment не осталось.
    op.execute("DELETE FROM escalations WHERE assessment_id IS NULL")
    op.alter_column("escalations", "assessment_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_index(op.f("ix_checkin_sessions_patient_id"), table_name="checkin_sessions")
    op.drop_table("checkin_sessions")
