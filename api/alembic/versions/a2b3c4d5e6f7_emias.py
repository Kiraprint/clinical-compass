"""FR-25: интеграция с ЕМИАС — связки, история назначений, журнал прогонов

Revision ID: a2b3c4d5e6f7
Revises: c1d2e3f4a5b6
Create Date: 2026-10-22 12:00:00.000000

EmiasLink — 1:1 связка с пациентом ЕМИАС (СНИЛС только как sha256+перец);
PatientMedication — история назначений (title/dosage шифруются pgcrypto, как
остальная спецкатегория ПДн); EmiasSyncRun — воспроизводимость прогонов.
Приёмы ЕМИАС живут в visits: source='emias', clinician_id NULL, emias_id unique —
идемпотентный upsert при повторной синхронизации.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # visits: приёмы, импортированные из ЕМИАС (врача-источника локально нет).
    op.add_column(
        "visits",
        sa.Column("source", sa.String(length=8), server_default=sa.text("'local'"), nullable=False),
    )
    op.add_column("visits", sa.Column("emias_id", sa.String(length=64), nullable=True))
    op.alter_column("visits", "clinician_id", existing_type=sa.Uuid(), nullable=True)
    op.create_unique_constraint("visits_emias_id_key", "visits", ["emias_id"])
    op.create_check_constraint("visits_source", "visits", "source in ('local','emias')")

    op.create_table(
        "emias_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("emias_patient_id", sa.String(length=64), nullable=False),
        sa.Column("snils_hash", sa.String(length=64), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("patient_id"),
        sa.UniqueConstraint("emias_patient_id"),
    )
    op.create_index(op.f("ix_emias_links_snils_hash"), "emias_links", ["snils_hash"])

    op.create_table(
        "patient_medications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.LargeBinary(), nullable=False),
        sa.Column("dosage", sa.LargeBinary(), nullable=True),
        sa.Column("started_on", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("emias_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status in ('active','completed','stopped','unknown')",
            name="patient_medications_status",
        ),
        sa.ForeignKeyConstraint(["patient_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("emias_id"),
    )
    op.create_index(op.f("ix_patient_medications_patient_id"), "patient_medications", ["patient_id"])

    op.create_table(
        "emias_sync_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('running','ok','partial','failed')", name="emias_sync_runs_status"),
        sa.CheckConstraint("mode in ('patients','one')", name="emias_sync_runs_mode"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_emias_sync_runs_started_at", "emias_sync_runs", ["started_at"])

def downgrade() -> None:
    op.drop_index("ix_emias_sync_runs_started_at", table_name="emias_sync_runs")
    op.drop_table("emias_sync_runs")
    op.drop_index(op.f("ix_patient_medications_patient_id"), table_name="patient_medications")
    op.drop_table("patient_medications")
    op.drop_index(op.f("ix_emias_links_snils_hash"), table_name="emias_links")
    op.drop_table("emias_links")
    # Импортированные из ЕМИАС визиты без source/emias_id не представимы и имеют
    # clinician_id = NULL: удаляем их ДО ужесточения NOT NULL, иначе downgrade
    # падает на первой же синхронизированной записи (P2-1 ревью).
    op.execute("DELETE FROM visits WHERE source = 'emias'")
    op.drop_constraint("visits_source", "visits", type_="check")
    op.drop_constraint("visits_emias_id_key", "visits", type_="unique")
    op.alter_column("visits", "clinician_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("visits", "emias_id")
    op.drop_column("visits", "source")
