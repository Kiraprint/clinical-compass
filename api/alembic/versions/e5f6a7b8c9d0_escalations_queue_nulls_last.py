"""escalations_open_queue: NULLS LAST, как в ORM-модели

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-24

Индекс очереди создавался как `(created_at DESC)`, а модель — как
`(resolved_at, created_at)`; запрос сортирует `created_at DESC NULLS LAST`, и
`created_at` в таблице nullable, поэтому ни один из вариантов не обслуживал
сортировку. Приводим индекс и модель к одному определению
`(created_at DESC NULLS LAST) WHERE resolved_at IS NULL`.
"""

from alembic import op
import sqlalchemy as sa

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

_INDEX = "escalations_open_queue"


def upgrade() -> None:
    op.drop_index(_INDEX, table_name="escalations")
    op.create_index(
        _INDEX,
        "escalations",
        [sa.text("created_at DESC NULLS LAST")],
        unique=False,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="escalations")
    op.create_index(
        _INDEX,
        "escalations",
        [sa.text("created_at DESC")],
        unique=False,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
