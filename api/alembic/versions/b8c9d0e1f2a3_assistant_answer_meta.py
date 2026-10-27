"""assistant_messages.answer_meta: признак «ИИ не сработал» и фрагменты

Детерминированный ответ по фрагментам раньше выглядел в UI как ответ ИИ:
врач видел сырую выдачу сниппетов и не понимал, что модель не ответила.
`answer_meta` хранит `{"answer_kind": "llm"|"fragments", "llm_reason":
"ok"|"disabled"|"timeout"|"empty"|"http_error", "fragments": [{n,
source_title, text}]}` ровно в том виде, в котором ответ ушёл клиенту, чтобы
thread-detail после перезагрузки страницы отдавал тот же контракт.

Текст фрагментов — публичные чанки КБ (не ПДн), поэтому шифрование не нужно;
карта пациента фрагментом не является и сюда не попадает.

Downgrade: колонка удаляется; сами сообщения и цитаты остаются.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b8c9d0e1f2a3"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_messages",
        sa.Column(
            "answer_meta",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("assistant_messages", "answer_meta")
