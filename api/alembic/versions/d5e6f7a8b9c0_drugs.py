"""drugs reference table

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-10-19

Справочник препаратов (источник — ГРЛС). Не ПДн. Чанки ИИ-агента в Qdrant
строятся из этой таблицы (api/app/kb_seed._drug_docs). Данные вносятся seed.py
из api/app/data/drugs.json (idempotent upsert по drug_id).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "drugs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("drug_id", sa.String(64), nullable=False),
        sa.Column("trade_name", sa.String(128), nullable=False),
        sa.Column("inn", sa.String(128), nullable=False),
        sa.Column("inn_en", sa.String(128), nullable=True),
        sa.Column("form", sa.String(255), nullable=False),
        sa.Column("reg_certificate", sa.String(128), nullable=False),
        sa.Column("reg_date", sa.Date, nullable=False),
        sa.Column("revision_date", sa.Date, nullable=True),
        sa.Column("pharmacology", sa.Text, nullable=False),
        sa.Column("indications", sa.Text, nullable=False),
        sa.Column("dosage", sa.Text, nullable=False),
        sa.Column("contraindications", sa.Text, nullable=False),
        sa.Column("adverse_events", sa.Text, nullable=False),
        sa.Column("special_notes", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("drugs_drug_id", "drugs", ["drug_id"], unique=True)


def downgrade() -> None:
    op.drop_index("drugs_drug_id", table_name="drugs")
    op.drop_table("drugs")
