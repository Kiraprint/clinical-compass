"""assistant threads: сессии врача с контекстом и мультипациентской областью

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-10-22 12:00:00.000000

FR-21: тред ассистента хранит историю диалога между запросами. Текст сообщений —
спецкатегория ПДн → bytea (pgp_sym_encrypt, CC_DATA_KEY), как diary/chat.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.create_table(
        "assistant_threads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("clinician_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        # Заголовок — свободный текст врача (может содержать ФИО) → pgcrypto.
        sa.Column("title", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_assistant_threads_clinician_id", "assistant_threads", ["clinician_id"])
    op.create_index("ix_assistant_threads_updated_at", "assistant_threads", ["updated_at"])

    op.create_table(
        "assistant_thread_patients",
        sa.Column(
            "thread_id",
            sa.Uuid(),
            sa.ForeignKey("assistant_threads.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
    )

    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "thread_id",
            sa.Uuid(),
            sa.ForeignKey("assistant_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("topic", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role in ('CLINICIAN','ASSISTANT')", name="assistant_messages_role"),
    )
    op.create_index("ix_assistant_messages_thread_id", "assistant_messages", ["thread_id"])
    op.create_index("ix_assistant_messages_created_at", "assistant_messages", ["created_at"])
    op.create_index(
        "ix_assistant_messages_thread_created", "assistant_messages", ["thread_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("assistant_messages")
    op.drop_table("assistant_thread_patients")
    op.drop_table("assistant_threads")
