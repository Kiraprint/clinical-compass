"""clinical_sessions.ai_status: промежуточный статус `diarizing`

Расшифровка разделена на два этапа (docs/18 §4.3): ASR отдаёт текст, затем
диаризация добавляет метки спикеров. Между этапами статус `diarizing` — текст
уже сохранён и врач его читает, а «идёт расшифровка» больше не соответствует
действительности. Без нового значения CHECK не пускал бы такое состояние.

`String(16)` не меняем: `diarizing` — 9 символов.

Downgrade: сессии в `diarizing` переводятся в `transcribed` (текст у них есть),
затем CHECK возвращается к прежнему списку значений.
"""

from __future__ import annotations

from alembic import op

revision = "c9e0f1a2b3c4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None

OLD = "ai_status in ('off','recording','transcribing','transcribed','failed')"
NEW = "ai_status in ('off','recording','transcribing','diarizing','transcribed','failed')"


def upgrade() -> None:
    op.drop_constraint("clinical_sessions_ai_status", "clinical_sessions", type_="check")
    op.create_check_constraint("clinical_sessions_ai_status", "clinical_sessions", NEW)


def downgrade() -> None:
    # Текст у таких сессий уже записан — `transcribed` честнее, чем failed.
    op.execute(
        "update clinical_sessions set ai_status = 'transcribed' "
        "where ai_status = 'diarizing'"
    )
    op.drop_constraint("clinical_sessions_ai_status", "clinical_sessions", type_="check")
    op.create_check_constraint("clinical_sessions_ai_status", "clinical_sessions", OLD)
