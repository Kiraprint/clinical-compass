"""adverse_events table (задача кейса 3 — НЯ)

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-10-18
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b3c4d5e6f7a8"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "adverse_events",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("patient_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("clinician_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("event_text", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("onset_date", sa.Date(), nullable=False),
        sa.Column("suspected_drug", sa.String(128), nullable=True),
        sa.Column("action_taken", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False, index=True),
        sa.CheckConstraint("severity in ('mild','moderate','severe')", name="adverse_events_severity"),
        sa.CheckConstraint(
            "action_taken is null or action_taken in ('none','monitoring','dose_reduced','stopped','referred')",
            name="adverse_events_action",
        ),
    )


def downgrade() -> None:
    op.drop_table("adverse_events")
