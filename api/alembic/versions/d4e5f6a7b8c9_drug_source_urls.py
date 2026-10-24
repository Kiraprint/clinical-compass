"""Справочник препаратов: провенанс источников (source_urls)

Revision ID: d4e5f6a7b8c9
Revises: d1a2b3c4e5f6
Create Date: 2026-10-24

Расширение справочника drugs до трёх препаратов (Авиандр, Золофт, Эсциталопрам
Канон) требует хранить ссылки на первоисточники по каждому препарату: запись в
ГРЛС и инструкцию по медицинскому применению. Раньше источник был только
строкой на уровне файла api/app/data/drugs.json и терялся при засеве.

  * source_urls — JSONB-массив абсолютных URL, по умолчанию пустой;
  * данные не ПДн (публичные факты регистрационного удостоверения), шифрование
    не нужно;
  * обратного заполнения нет: сид перезаписывает строки из drugs.json.

Обратимость: downgrade удаляет колонку; провенанс восстанавливается повторным
`python -m app.seed` из drugs.json.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d4e5f6a7b8c9"
down_revision = "d1a2b3c4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "drugs",
        sa.Column(
            "source_urls",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("drugs", "source_urls")
