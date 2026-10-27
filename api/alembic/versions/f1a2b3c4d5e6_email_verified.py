"""email verification

FR-48 (v2): подтверждение адреса при регистрации — мягкая метка
`users.email_verified_at` (баннер, без блокировки входа) и purpose `verify`
в auth_codes.

Уже существующие учётные записи помечаются подтверждёнными: они заведены до
появления проверки адреса (демо-сид, стенды), и вешать им баннер «подтвердите
почту» задним числом бессмысленно — кода у них на руках нет.

Revision ID: f1a2b3c4d5e6
Revises: c1a2b3d4e5f6
Create Date: 2026-10-27 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'c1a2b3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_PURPOSES = "purpose in ('reset','login')"
NEW_PURPOSES = "purpose in ('reset','login','verify')"


def upgrade() -> None:
    op.add_column(
        'users', sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True)
    )
    # Бэкфилл: адреса, существовавшие до этой проверки, считаем подтверждёнными.
    op.execute("update users set email_verified_at = coalesce(created_at, now())")
    op.drop_constraint('auth_codes_purpose', 'auth_codes', type_='check')
    op.create_check_constraint('auth_codes_purpose', 'auth_codes', NEW_PURPOSES)


def downgrade() -> None:
    # Коды подтверждения адреса старый CHECK не пропустит — их надо убрать,
    # иначе downgrade падает на constraint violation.
    op.execute("delete from auth_codes where purpose = 'verify'")
    op.drop_constraint('auth_codes_purpose', 'auth_codes', type_='check')
    op.create_check_constraint('auth_codes_purpose', 'auth_codes', OLD_PURPOSES)
    op.drop_column('users', 'email_verified_at')
