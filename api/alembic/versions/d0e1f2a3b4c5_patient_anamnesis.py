"""FR-37: анамнез пациента — вводные врача и сформированный текст

Revision ID: d0e1f2a3b4c5
Revises: b7c8d9e0f1a2
Create Date: 2026-10-25

Одна строка на пациента (UNIQUE patient_id). `brief` (краткие вводные) и
`text` (структурированный анамнез) — спецкатегория ПДн, поэтому bytea +
pgp_sym_encrypt (api/app/crypto.EncryptedText), ключ CC_DATA_KEY.
created_by/updated_by — кто создал и последним правил (FK users.id);
updated_at — время последнего сохранения через PUT.

Обратимость: downgrade удаляет таблицу целиком. Анамнез можно восстановить
повторным вводом/формированием черновика, других потребителей у таблицы нет.
"""
import sqlalchemy as sa
from alembic import op

revision = "d0e1f2a3b4c5"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.create_table(
        "patient_anamnesis",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "patient_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False, unique=True
        ),
        # bytea: DDL EncryptedText (pgp_sym_encrypt, ключ CC_DATA_KEY).
        sa.Column("brief", sa.LargeBinary(), nullable=True),
        sa.Column("text", sa.LargeBinary(), nullable=True),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )


def downgrade() -> None:
    op.drop_table("patient_anamnesis")
