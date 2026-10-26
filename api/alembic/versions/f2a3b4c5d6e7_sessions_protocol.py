"""FR-38/39: клинические сессии врача — календарь, сессия, протокол, артефакты

Revision ID: f2a3b4c5d6e7
Revises: c9d0e1f2a3b4
Create Date: 2026-10-26

Схема контракта docs/18 §3 (PR P1). Пять таблиц и одна колонка:

- `appointments` — план приёма (пересечения ловит сервис под FOR UPDATE,
  btree_gist/exclusion не вводим — docs/18 §3.1);
- `clinical_sessions` — факт приёма; одна сессия на запись календаря (partial
  unique по appointment_id), offline-сессии живут с appointment_id = NULL;
- `session_protocols` — версии протокола, `sections_cipher` — bytea (pgcrypto,
  EncryptedText, ключ CC_DATA_KEY): канонический JSON §5 свободнотекстовый;
- `document_artifacts` — метаданные шифрованных файлов (AES-256-GCM на диске,
  sha256 по шифротексту), создаётся до `session_transcripts` (FK на артефакт);
- `session_transcripts` — метаданные расшифровки (PR P3): текст только в артефакте;
- `visits.session_id` — идемпотентная связь «завершение сессии → приём»
  (UNIQUE, NULL для приёмов без сессии и импорта из ЕМИАС).

Обратимость: downgrade удаляется в обратном порядке и снимает колонку
`visits.session_id`; данные PR P1 (сессии/протоколы/артефакты) ещё не имеют
потребителей вне этого контура, восстановление — повторный ввод врачом.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.create_table(
        "appointments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("clinician_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "duration_min", sa.Integer(), nullable=False, server_default=sa.text("50")
        ),
        sa.Column(
            "channel", sa.String(length=16), nullable=False, server_default=sa.text("'in_person'")
        ),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'planned'")
        ),
        sa.Column("location", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.CheckConstraint("duration_min between 10 and 480", name="appointments_duration"),
        sa.CheckConstraint(
            "channel in ('in_person','online','phone')", name="appointments_channel"
        ),
        sa.CheckConstraint(
            "status in ('planned','confirmed','completed','cancelled','no_show')",
            name="appointments_status",
        ),
    )
    op.create_index("appointments_clinician_starts", "appointments", ["clinician_id", "starts_at"])
    op.create_index("appointments_patient_starts", "appointments", ["patient_id", "starts_at"])

    op.create_table(
        "clinical_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "appointment_id", sa.Uuid(), sa.ForeignKey("appointments.id"), nullable=True
        ),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("clinician_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "channel", sa.String(length=16), nullable=False, server_default=sa.text("'in_person'")
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column(
            "audio_consent", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "ai_status", sa.String(length=16), nullable=False, server_default=sa.text("'off'")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.CheckConstraint(
            "channel in ('in_person','online','phone')", name="clinical_sessions_channel"
        ),
        sa.CheckConstraint("status in ('active','finished')", name="clinical_sessions_status"),
        sa.CheckConstraint(
            "ai_status in ('off','recording','transcribing','transcribed','failed')",
            name="clinical_sessions_ai_status",
        ),
        sa.CheckConstraint(
            "ended_at is null or ended_at >= started_at", name="clinical_sessions_interval"
        ),
    )
    op.create_index(
        "clinical_sessions_appointment",
        "clinical_sessions",
        ["appointment_id"],
        unique=True,
        postgresql_where=sa.text("appointment_id is not null"),
    )
    op.create_index(
        "clinical_sessions_clinician_started",
        "clinical_sessions",
        ["clinician_id", "started_at"],
    )
    op.create_index(
        "clinical_sessions_patient_started", "clinical_sessions", ["patient_id", "started_at"]
    )

    op.create_table(
        "session_protocols",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_id", sa.Uuid(), sa.ForeignKey("clinical_sessions.id"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'draft'")
        ),
        sa.Column(
            "source", sa.String(length=16), nullable=False, server_default=sa.text("'manual'")
        ),
        # bytea: DDL EncryptedText (pgp_sym_encrypt, ключ CC_DATA_KEY).
        sa.Column("sections_cipher", sa.LargeBinary(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("diagnosis_code", sa.String(length=16), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=16), nullable=True),
        sa.Column("needs_review", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("approved_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.CheckConstraint(
            "status in ('draft','approved','signed','submitted','superseded','rejected')",
            name="session_protocols_status",
        ),
        sa.CheckConstraint("source in ('manual','llm')", name="session_protocols_source"),
        sa.UniqueConstraint("session_id", "version", name="session_protocols_session_version"),
    )
    op.create_index(
        "session_protocols_session_status", "session_protocols", ["session_id", "status"]
    )

    op.create_table(
        "document_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column(
            "session_id", sa.Uuid(), sa.ForeignKey("clinical_sessions.id"), nullable=True
        ),
        sa.Column(
            "protocol_id", sa.Uuid(), sa.ForeignKey("session_protocols.id"), nullable=True
        ),
        sa.Column("path", sa.String(length=400), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mime", sa.String(length=128), nullable=False),
        sa.Column("encrypted", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.CheckConstraint(
            "kind in ('audio','transcript','protocol_pdf','protocol_cda','signature')",
            name="document_artifacts_kind",
        ),
    )
    op.create_index("ix_document_artifacts_session_id", "document_artifacts", ["session_id"])

    op.create_table(
        "session_transcripts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_id", sa.Uuid(), sa.ForeignKey("clinical_sessions.id"), nullable=False, unique=True
        ),
        sa.Column(
            "language", sa.String(length=2), nullable=False, server_default=sa.text("'ru'")
        ),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column(
            "segments_artifact_id",
            sa.Uuid(),
            sa.ForeignKey("document_artifacts.id"),
            nullable=True,
        ),
        sa.Column("mask_categories", postgresql.JSONB(), nullable=True),
        sa.Column(
            "depersonalized", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )

    # docs/18 §3.3: завершение сессии создаёт ровно один приём.
    op.add_column("visits", sa.Column("session_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "visits_session_id_fkey", "visits", "clinical_sessions", ["session_id"], ["id"]
    )
    op.create_unique_constraint("visits_session_id_key", "visits", ["session_id"])


def downgrade() -> None:
    op.drop_constraint("visits_session_id_key", "visits", type_="unique")
    op.drop_constraint("visits_session_id_fkey", "visits", type_="foreignkey")
    op.drop_column("visits", "session_id")
    op.drop_table("session_transcripts")
    op.drop_index("ix_document_artifacts_session_id", table_name="document_artifacts")
    op.drop_table("document_artifacts")
    op.drop_index("session_protocols_session_status", table_name="session_protocols")
    op.drop_table("session_protocols")
    op.drop_index("clinical_sessions_patient_started", table_name="clinical_sessions")
    op.drop_index("clinical_sessions_clinician_started", table_name="clinical_sessions")
    op.drop_index("clinical_sessions_appointment", table_name="clinical_sessions")
    op.drop_table("clinical_sessions")
    op.drop_index("appointments_patient_starts", table_name="appointments")
    op.drop_index("appointments_clinician_starts", table_name="appointments")
    op.drop_table("appointments")
