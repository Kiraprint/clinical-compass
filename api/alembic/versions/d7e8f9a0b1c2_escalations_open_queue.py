"""escalations_open_queue: индекс очереди открытых сигналов (docs/16 §3).

Очередь читается как «resolved_at IS NULL, created_at >= now() - 90 дней» и
сортируется по created_at desc — под это и строим частичный индекс.

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-10-23
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "d7e8f9a0b1c2"
down_revision: Union[str, None] = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "escalations_open_queue",
        "escalations",
        ["resolved_at", "created_at"],
        unique=False,
        postgresql_where="resolved_at IS NULL",
    )


def downgrade() -> None:
    op.drop_index("escalations_open_queue", table_name="escalations")
