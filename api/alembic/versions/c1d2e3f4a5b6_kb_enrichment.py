"""kb-enrichment: реестр внешних источников, прогоны, персональные исключения

Revision ID: c1d2e3f4a5b6
Revises: b1c2d3e4f5a6
Create Date: 2026-10-21 16:20:00.000000

Qdrant остаётся производным индексом: kb_works — источник истины и провенанс
(статус ревью, версия, хеш контента, критерии, оценка методологии).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'kb_works',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('external_id', sa.String(length=64), nullable=False),
        sa.Column('guideline_id', sa.String(length=96), nullable=False),
        sa.Column('revision', sa.String(length=64), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('full_text', sa.Text(), nullable=False),
        sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('design', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('criteria', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('appraisal', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('retraction', sa.String(length=16), nullable=False),
        sa.Column('revision_history', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('indexed_hash', sa.String(length=64), nullable=True),
        sa.Column('chunk_count', sa.Integer(), nullable=False),
        sa.Column('approved_by', sa.Uuid(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reject_reason', sa.Text(), nullable=True),
        sa.CheckConstraint("kind in ('article','guideline')", name='kb_works_kind'),
        sa.CheckConstraint(
            "status in ('candidate','approved','rejected','excluded','superseded')",
            name='kb_works_status',
        ),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'external_id', name='kb_works_source_external'),
        sa.UniqueConstraint('guideline_id', name='kb_works_guideline_id'),
    )
    op.create_index('ix_kb_works_status', 'kb_works', ['status'], unique=False)

    op.create_table(
        'kb_runs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('trigger', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('query_plan', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('versions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('stats', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('errors', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "status in ('running','ok','partial','failed')", name='kb_runs_status'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_kb_runs_started_at', 'kb_runs', ['started_at'], unique=False)

    op.create_table(
        'kb_exclusions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('guideline_id', sa.String(length=96), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'guideline_id', name='kb_exclusions_user_guideline'),
    )
    op.create_index('ix_kb_exclusions_user_id', 'kb_exclusions', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_kb_exclusions_user_id', table_name='kb_exclusions')
    op.drop_table('kb_exclusions')
    op.drop_index('ix_kb_runs_started_at', table_name='kb_runs')
    op.drop_table('kb_runs')
    op.drop_index('ix_kb_works_status', table_name='kb_works')
    op.drop_table('kb_works')
