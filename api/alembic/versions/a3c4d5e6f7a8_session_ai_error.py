"""clinical_sessions.ai_error: код сбоя ИИ-конвейера сессии (docs/18 §4.3, PR P3)

Revision ID: a3c4d5e6f7a8
Revises: f2a3b4c5d6e7
Create Date: 2026-10-26 06:10:00.000000

Сбой расшифровки виден врачу в detail сессии (`ai_status='failed'` + код):
AUDIO_MISSING, TRANSCRIPT_TOO_LARGE, ML_UNAVAILABLE, DEPERSONALIZATION_UNAVAILABLE,
TRANSCRIBE_FAILED. Текста и диагностики сайдкара в колонке нет — только код.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3c4d5e6f7a8"
down_revision: Union[str, None] = "c8a1d2e3f4b5"  # P4 (ЭЦП) → P5 (РЭМД): обе ветки после P1
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("clinical_sessions", sa.Column("ai_error", sa.String(length=48), nullable=True))


def downgrade() -> None:
    op.drop_column("clinical_sessions", "ai_error")
