"""FR-36: персональное ревью статьи врачом — видимость в выдаче ассистента

Revision ID: c9d0e1f2a3b4
Revises: b7c8d9e0f1a2
Create Date: 2026-10-25

Публикация статьи в индекс (`kb_works.status='approved'`, админский approve)
остаётся глобальной, а видимость в ответах ассистента — персональной: врач
видит статью, только если сам поставил `decision='approved'`. Записи нет —
статья этому врачу не видна; гайдлайны ВОЗ (kind='guideline') видны всем без
решения (docs/15 §12).

Обе связи ON DELETE CASCADE: удаление врача или записи реестра не оставляет
висячих решений. Отзыв решения = DELETE строки (эндпоинт меняет decision,
«сбросить в new» не требуется продуктом).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kb_work_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("clinician_id", sa.Uuid(), nullable=False),
        sa.Column("work_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision in ('approved','rejected')", name="kb_work_approvals_decision"
        ),
        sa.ForeignKeyConstraint(["clinician_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["kb_works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinician_id", "work_id", name="kb_work_approvals_clinician_work"),
    )
    op.create_index(
        "ix_kb_work_approvals_clinician_id", "kb_work_approvals", ["clinician_id"], unique=False
    )
    op.create_index(
        "ix_kb_work_approvals_work_id", "kb_work_approvals", ["work_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_kb_work_approvals_work_id", table_name="kb_work_approvals")
    op.drop_index("ix_kb_work_approvals_clinician_id", table_name="kb_work_approvals")
    op.drop_table("kb_work_approvals")
