"""session_transcripts: разметка реплик по спикерам (FR-42, docs/18 §3.5, PR P7)

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-10-26 18:10:00.000000

Транскрипт получает роли участников приёма: ML-сайдкар отдаёт кластеры
(``A``/``B``), api назначает роли по умолчанию (первый заговоривший — врач) и
разрешает врачу переставить их PATCH'ем.

- ``diarized`` — сайдкар реально разделил говорящих (иначе честная деградация);
- ``speaker_roles`` — ``{"A": "clinician", "B": "patient"}`` или NULL, когда
  кластеров не два (тогда роли не назначаем: угадывание — клиническая ошибка);
- ``speaker_roles_assumed`` — роли назначены по умолчанию и не подтверждены
  врачом. PATCH ставит false.

Текст реплик миграция не трогает: он и раньше жил только в шифрованном
артефакте, роли в артефакте сегментов переписываются тем же PATCH'ем.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session_transcripts",
        sa.Column("diarized", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "session_transcripts",
        sa.Column("speaker_roles", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "session_transcripts",
        sa.Column(
            "speaker_roles_assumed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("session_transcripts", "speaker_roles_assumed")
    op.drop_column("session_transcripts", "speaker_roles")
    op.drop_column("session_transcripts", "diarized")
