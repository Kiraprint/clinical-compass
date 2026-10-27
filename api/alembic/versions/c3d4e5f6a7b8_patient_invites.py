"""patient invites: карта от врача + одноразовая ссылка (FR-49)

Врач заводит карту до появления аккаунта; `users`-строка создаётся только при
активации, поэтому «полупустых» аккаунтов с выдуманным адресом нет.

`token_hash` — sha256(token + CC_DATA_KEY): сам токен не хранится, ссылку
нельзя показать повторно (перевыпуск выдаёт новый). `phone` — ПДн, поэтому
bytea + pgp_sym_encrypt (api/app/crypto.EncryptedText, ключ CC_DATA_KEY).

Downgrade: удаляет приглашения целиком. Уже принятые приглашения ссылаются на
живые аккаунты пациентов, но сами аккаунты, ростер и медицинские данные этой
таблицей не создавались — они остаются.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "patient_invites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "clinician_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.CHAR(64), nullable=False, unique=True),
        sa.Column("fio", sa.String(255), nullable=True),
        sa.Column("phone", sa.LargeBinary(), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "accepted_patient_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")
        ),
    )
    op.create_index("ix_patient_invites_clinician_id", "patient_invites", ["clinician_id"])
    op.create_index(
        "patient_invites_clinician", "patient_invites", ["clinician_id", "accepted_at"]
    )
    op.create_index("patient_invites_expires", "patient_invites", ["expires_at"])


def downgrade() -> None:
    op.drop_index("patient_invites_expires", table_name="patient_invites")
    op.drop_index("patient_invites_clinician", table_name="patient_invites")
    op.drop_index("ix_patient_invites_clinician_id", table_name="patient_invites")
    op.drop_table("patient_invites")
