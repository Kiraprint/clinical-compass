"""adverse_events.severity: NULL до проверки врачом (FR-33/34)

Revision ID: b7c8d9e0f1a2
Revises: e5f6a7b8c9d0
Create Date: 2026-10-25

Пациент больше не оценивает тяжесть нежелательного явления: в форме остаётся
только описание, а шкалу mild/moderate/severe выставляет врач при проверке.
Исторические строки (в т.ч. все врачебные НЯ) сохраняют прежнее значение —
колонка просто перестаёт быть NOT NULL.

Downgrade: если есть строки с severity IS NULL, откат падает с ошибкой — это
НЯ, которые пациент сообщил, а врач ещё не оценил. Подставить им 'mild' значило
бы выдумать клиническую оценку; такие строки нужно разобрать вручную.
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
    """Возвращает NOT NULL только если ни одно НЯ не осталось без оценки врача."""
    conn = op.get_bind()
    unassessed = conn.execute(
        sa.text("select count(*) from adverse_events where severity is null")
    ).scalar_one()
    if unassessed:
        raise RuntimeError(
            f"нельзя вернуть adverse_events.severity NOT NULL: {unassessed} строк без оценки "
            "(severity IS NULL). Подстановка 'mild' выдумала бы клиническую оценку — "
            "оцените эти НЯ врачом и повторите downgrade."
        )
    op.alter_column(
        "adverse_events",
        "severity",
        existing_type=sa.String(length=16),
        nullable=False,
    )
