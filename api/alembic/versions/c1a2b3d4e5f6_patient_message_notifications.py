"""patient message notifications

FR-10: сообщение пациента в чате создаёт уведомление врачам roster.
Новый kind PATIENT_MESSAGE + частичный уникальный индекс, который держит
не более одного непрочитанного уведомления на (врач, пациент).

Revision ID: c1a2b3d4e5f6
Revises: b7c9d1e3f5a2
Create Date: 2026-10-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c1a2b3d4e5f6'
down_revision: Union[str, None] = 'b7c9d1e3f5a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_KINDS = "kind in ('RED_ESCALATION','REMINDER','ADVERSE_EVENT')"
NEW_KINDS = "kind in ('RED_ESCALATION','REMINDER','ADVERSE_EVENT','PATIENT_MESSAGE')"

def upgrade() -> None:
    op.drop_constraint('notifications_kind', 'notifications', type_='check')
    op.create_check_constraint('notifications_kind', 'notifications', NEW_KINDS)
    op.create_index(
        'notifications_chat_unread', 'notifications', ['user_id', 'patient_id'],
        unique=True,
        postgresql_where=sa.text("kind = 'PATIENT_MESSAGE' AND read_at IS NULL"),
    )

def downgrade() -> None:
    op.drop_index('notifications_chat_unread', table_name='notifications')
    op.drop_constraint('notifications_kind', 'notifications', type_='check')
    op.create_check_constraint('notifications_kind', 'notifications', OLD_KINDS)
