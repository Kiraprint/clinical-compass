"""FR-31: факты из переписки с пациентом — черновики с подтверждением врача

Revision ID: c8d9e0f1a2b3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-24

chat_insights: LLM извлекает из переписки только явные высказывания пациента;
врач принимает или отклоняет факт, в карточку попадают только accepted.
  * text — спецкатегория ПДн, bytea (pgcrypto, EncryptedText);
  * kind/status — CHECK-домены (symptom|medication|adherence|sleep|substance|risk|other;
    pending|accepted|rejected);
  * dedupe_hash — sha256(patient_id|kind|нормализованный text); UNIQUE(patient_id,
    dedupe_hash) — повторный анализ не создаёт дубликатов;
  * source_message_ids — id сообщений-оснований (JSONB, текстов не содержит);
  * индекс (patient_id, status) — панель pending и accepted для карточки.

Обратимость: downgrade удаляет таблицу целиком; черновики восстанавливаются
повторным анализом переписки, других потребителей у таблицы нет.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c8d9e0f1a2b3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.create_table(
        "chat_insights",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(16), nullable=False),
        # bytea: DDL EncryptedText (pgp_sym_encrypt, ключ CC_DATA_KEY).
        sa.Column("text", sa.LargeBinary(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "source_message_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("dedupe_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("reviewed_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind in ('symptom','medication','adherence','sleep','substance','risk','other')",
            name="chat_insights_kind",
        ),
        sa.CheckConstraint(
            "status in ('pending','accepted','rejected')", name="chat_insights_status"
        ),
        sa.UniqueConstraint("patient_id", "dedupe_hash", name="chat_insights_patient_dedupe"),
    )
    op.create_index("ix_chat_insights_patient_status", "chat_insights", ["patient_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_chat_insights_patient_status", table_name="chat_insights")
    op.drop_table("chat_insights")
