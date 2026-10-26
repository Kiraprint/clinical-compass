"""clinical_sessions.ai_started_at: момент запуска расшифровки (docs/18 §4.3, PR P3)

Revision ID: b4c5d6e7f8a9
Revises: a3c4d5e6f7a8
Create Date: 2026-10-26 07:20:00.000000

Фоновая расшифровка живёт в процессе воркера: после рестарта/падения строки
остаются в `ai_status='transcribing'` навсегда. Колонка позволяет реаперу
(`services.session_ai.reap_stale`) отличить живую длинную запись от зависшей:
порог — `CC_SESSION_AI_TIMEOUT × 2`.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, None] = "a3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clinical_sessions",
        sa.Column("ai_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("clinical_sessions", "ai_started_at")
