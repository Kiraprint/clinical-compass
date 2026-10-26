"""FR-40: подписи протокола приёма (docs/18 §3.7, §7)

Revision ID: b7c3d4e5f6a1
Revises: f2a3b4c5d6e7
Create Date: 2026-10-26

Одна таблица: `protocol_signatures`. Две строки на подписанный протокол —
`clinician` (врач) и `mo` (уполномоченное лицо МО, п. 11 приказа 209н).
Закрытых ключей здесь нет: в `cert_der` только публичный сертификат, сама
CMS-подпись — в шифрованном артефакте `document_artifacts(kind='signature')`.

Обратимость: `downgrade` удаляет таблицу. Артефакты подписей при этом остаются
на диске (они принадлежат протоколу, а не таблице) — повторная подпись после
откатa возможна только для новой версии протокола, см. §3.4.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7c3d4e5f6a1"
down_revision: Union[str, None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "protocol_signatures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "protocol_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("session_protocols.id"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clinical_sessions.id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column(
            "signer_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("fio", sa.String(200), nullable=False),
        sa.Column("position", sa.String(200), nullable=False),
        sa.Column("organization", sa.String(200), nullable=False),
        sa.Column("snils", sa.String(16), nullable=True),
        sa.Column("cert_serial", sa.String(64), nullable=False),
        sa.Column("cert_issuer", sa.String(400), nullable=False),
        sa.Column("cert_not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cert_not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cert_thumbprint", sa.CHAR(64), nullable=False),
        sa.Column("cert_der", sa.LargeBinary(), nullable=False),
        sa.Column(
            "cms_artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_artifacts.id"),
            nullable=False,
        ),
        sa.Column(
            "digest_algorithm",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'GOST3411-2012-256'"),
        ),
        sa.Column(
            "signature_algorithm",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'GOST3410-2012-256'"),
        ),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("role in ('clinician','mo')", name="protocol_signatures_role"),
        sa.UniqueConstraint("protocol_id", "role", name="protocol_signatures_protocol_role"),
    )
    op.create_index("protocol_signatures_session", "protocol_signatures", ["session_id", "role"])


def downgrade() -> None:
    op.drop_index("protocol_signatures_session", table_name="protocol_signatures")
    op.drop_table("protocol_signatures")
