"""adverse_events.severity: NULL до проверки врачом (FR-33/34)

Revision ID: b7c8d9e0f1a2
Revises: e5f6a7b8c9d0
Create Date: 2026-10-25

Пациент больше не оценивает тяжесть нежелательного явления: в форме остаётся
только описание, а шкалу mild/moderate/severe выставляет врач при проверке.
Исторические строки (в т.ч. все врачебные НЯ) сохраняют прежнее значение —
колонка просто перестаёт быть NOT NULL.

Downgrade: NULL-значения заполняются 'mild' (иначе NOT NULL не восстановить);
это единственная потеря информации на откате, upgrade данные не меняет.
"""

from alembic import op
import sqlalchemy as sa

revision = "b7c8d9e0f1a2"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "adverse_events",
        "severity",
        existing_type=sa.String(length=16),
        nullable=True,
    )


def downgrade() -> None:
    op.execute("UPDATE adverse_events SET severity = 'mild' WHERE severity IS NULL")
    op.alter_column(
        "adverse_events",
        "severity",
        existing_type=sa.String(length=16),
        nullable=False,
    )
