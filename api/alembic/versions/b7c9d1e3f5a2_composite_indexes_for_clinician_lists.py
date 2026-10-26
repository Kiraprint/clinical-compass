"""composite indexes for clinician lists

Покрывающие индексы под «тяжёлые» списки (docs/03 §12): уведомления и
назначения с фильтром по статусу. Для visits композитный индекс по EXPLAIN
не выигрывает (планировщик берёт ix_visits_visited_at/seq scan), поэтому он
здесь не создаётся.
Одноколоночные индексы, вошедшие в новый как префикс, удаляются — дублей нет.

Revision ID: b7c9d1e3f5a2
Revises: e2f3a4b5c6d7
Create Date: 2026-10-27 02:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b7c9d1e3f5a2'
down_revision: Union[str, None] = 'e2f3a4b5c6d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # user_id остаётся префиксом нового индекса — отдельный больше не нужен.
    op.create_index(
        'ix_notifications_user_created', 'notifications',
        ['user_id', sa.text('created_at DESC'), sa.text('id DESC')], unique=False,
    )
    op.drop_index(op.f('ix_notifications_user_id'), table_name='notifications')

    # Назначения пациента обычно фильтруются по статусу (active) — сразу в индексе.
    op.create_index(
        'ix_patient_medications_patient_status', 'patient_medications',
        ['patient_id', 'status'], unique=False,
    )
    op.drop_index(op.f('ix_patient_medications_patient_id'), table_name='patient_medications')


def downgrade() -> None:
    op.create_index(
        op.f('ix_patient_medications_patient_id'), 'patient_medications', ['patient_id'], unique=False
    )
    op.drop_index('ix_patient_medications_patient_status', table_name='patient_medications')

    op.create_index(op.f('ix_notifications_user_id'), 'notifications', ['user_id'], unique=False)
    op.drop_index('ix_notifications_user_created', table_name='notifications')
