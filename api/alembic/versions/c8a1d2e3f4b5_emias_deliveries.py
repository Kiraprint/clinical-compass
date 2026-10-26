"""FR-41: передачи подписанного СЭМД в РЭМД (docs/18 §3.8, §8)

Revision ID: c8a1d2e3f4b5
Revises: b7c3d4e5f6a1
Create Date: 2026-10-26

Одна таблица: `emias_deliveries` — по строке на версию протокола. Тело СЭМД и
подпись здесь не дублируются: они уже в `document_artifacts` и
`protocol_signatures`, а передача хранит только хэш отправленных байт, GUID
идемпотентности (`local_document_uid`), идентификаторы РЭМД и результат.

`downgrade` удаляет таблицу; артефакты и подписи остаются — повторная отправка
после откатa создаст новую строку с тем же детерминированным UID, и РЭМД
(мок) ответит тем же `request_id`, если документ не менялся.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c8a1d2e3f4b5"
down_revision: Union[str, None] = "b7c3d4e5f6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "emias_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clinical_sessions.id"),
            nullable=False,
        ),
        sa.Column(
            "protocol_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("session_protocols.id"),
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default=sa.text("'queued'")
        ),
        sa.Column("local_document_uid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_sha256", sa.CHAR(64), nullable=False),
        sa.Column("kind", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("external_id", sa.String(64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.String(400), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status in ('queued','sending','registered','rejected','failed')",
            name="emias_deliveries_status",
        ),
        sa.UniqueConstraint("protocol_id", name="emias_deliveries_protocol"),
    )
    op.create_index(
        "emias_deliveries_session", "emias_deliveries", ["session_id", "submitted_at"]
    )
    op.create_index("emias_deliveries_request", "emias_deliveries", ["request_id"])


def downgrade() -> None:
    op.drop_index("emias_deliveries_request", table_name="emias_deliveries")
    op.drop_index("emias_deliveries_session", table_name="emias_deliveries")
    op.drop_table("emias_deliveries")
