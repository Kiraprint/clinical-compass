"""auth_codes: одноразовые коды e-mail (FR-48, docs/03 §15)

Revision ID: e2f3a4b5c6d7
Revises: b7d1c9e0a4f2
Create Date: 2026-10-26 20:30:00.000000

Сброс пароля и подтверждение входа пациента по коду из письма. В таблице
только SHA-256(code + CC_DATA_KEY): сам код живёт в письме, поэтому dump БД
не даёт возможности подобрать действующий код быстрее, чем за 10 минут TTL и
5 попыток.

- ``purpose`` — ``reset`` | ``login`` (CHECK);
- ``attempts`` — бюджет перебора, инкрементируется на каждой проверке;
- ``consumed_at`` — одноразовость: успешная проверка закрывает строку;
- индекс ``(user_id, purpose, created_at DESC)`` — выборка последнего кода;
- ``user_id`` → ``users.id`` ON DELETE CASCADE: удаление аккаунта чистит коды.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, None] = "b7d1c9e0a4f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auth_codes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("purpose in ('reset','login')", name="auth_codes_purpose"),
    )
    op.create_index("ix_auth_codes_user_id", "auth_codes", ["user_id"])
    op.create_index(
        "auth_codes_user_purpose",
        "auth_codes",
        ["user_id", "purpose", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("auth_codes_user_purpose", table_name="auth_codes")
    op.drop_index("ix_auth_codes_user_id", table_name="auth_codes")
    op.drop_table("auth_codes")
