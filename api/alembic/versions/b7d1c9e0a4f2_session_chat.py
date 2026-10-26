"""session_chat_messages: диалог врача по сессии (FR-44, docs/18 §10, PR P9)

Revision ID: b7d1c9e0a4f2
Revises: c5d6e7f8a9b0
Create Date: 2026-10-26 21:30:00.000000

Одна таблица: реплики диалога «врач ↔ агент» вокруг приёма. Роль — только
``clinician`` или ``assistant`` (пациент в диалоге не участвует: это внутренний
инструмент врача, а не канал коммуникации с пациентом).

``content_cipher`` — bytea (pgcrypto, EncryptedText, ключ CC_DATA_KEY): врач
может назвать пациента, поэтому и вопрос, и ответ хранятся только шифротекстом.
Структурированная часть ответа ассистента (цитаты, ``proposal``) лежит в том же
шифрованном JSON — отдельных открытых jsonb-колонок намеренно нет.

Обратимость: ``downgrade`` удаляет таблицу. Диалог — производная от сессии и
протокола, поэтому потеря восполнима: врач продолжает разбор с нуля.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d1c9e0a4f2"
down_revision: Union[str, None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "session_chat_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("clinical_sessions.id"),
            nullable=False,
        ),
        sa.Column("clinician_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        # bytea: DDL EncryptedText (pgp_sym_encrypt, ключ CC_DATA_KEY).
        sa.Column("content_cipher", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "role in ('clinician','assistant')", name="session_chat_role"
        ),
    )
    op.create_index(
        "session_chat_messages_session_created",
        "session_chat_messages",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "session_chat_messages_session_created", table_name="session_chat_messages"
    )
    op.drop_table("session_chat_messages")
