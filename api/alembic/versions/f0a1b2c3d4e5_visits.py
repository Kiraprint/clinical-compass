"""visits: записи приёмов врач-пациент (задача кейса 6, п. «приём» в карточке)

Revision ID: f0a1b2c3d4e5
Revises: e9c0d1e2f3a4
Create Date: 2026-10-19 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f0a1b2c3d4e5'
down_revision: Union[str, None] = 'e9c0d1e2f3a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'visits',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('patient_id', sa.Uuid(), nullable=False),
        sa.Column('clinician_id', sa.Uuid(), nullable=False),
        sa.Column('visited_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('visit_type', sa.String(length=16), nullable=False),
        sa.Column('duration_min', sa.Integer(), nullable=True),
        sa.Column('notes', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("visit_type in ('in_person','online','phone')", name='visits_type'),
        sa.CheckConstraint('duration_min is null or (duration_min between 1 and 480)', name='visits_duration'),
        sa.ForeignKeyConstraint(['clinician_id'], ['users.id']),
        sa.ForeignKeyConstraint(['patient_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_visits_patient_id'), 'visits', ['patient_id'], unique=False)
    op.create_index(op.f('ix_visits_visited_at'), 'visits', ['visited_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_visits_visited_at'), table_name='visits')
    op.drop_index(op.f('ix_visits_patient_id'), table_name='visits')
    op.drop_table('visits')
