"""prescriptions (multi-scale)

Revision ID: a1b2c3d4e5f6
Revises: f55815ebac89
Create Date: 2026-10-18 03:00:00.000000

Замена единственной колонки users.prescribed_scale на множество prescriptions:
врач назначает пациенту несколько шкал скрининга; пациент выбирает одну из
назначенных на каждый прогон (один открытый скрининг на пациента).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'f55815ebac89'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'prescriptions',
        sa.Column('patient_id', sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), primary_key=True, nullable=False),
        sa.Column('scale_code', sa.String(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
    )
    # Перенос существующих назначений из users.prescribed_scale.
    op.execute(
        "INSERT INTO prescriptions (patient_id, scale_code) "
        "SELECT id, prescribed_scale FROM users WHERE prescribed_scale IS NOT NULL "
        "ON CONFLICT DO NOTHING"
    )
    op.drop_column('users', 'prescribed_scale')


def downgrade() -> None:
    op.add_column('users', sa.Column('prescribed_scale', sa.String(), nullable=True))
    op.execute(
        "UPDATE users SET prescribed_scale = sub.scale_code "
        "FROM (SELECT patient_id, scale_code FROM prescriptions) sub "
        "WHERE users.id = sub.patient_id"
    )
    op.drop_table('prescriptions')
