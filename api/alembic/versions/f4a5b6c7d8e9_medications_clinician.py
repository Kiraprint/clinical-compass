"""patient_medications: назначения врача в карточке (FR-23)

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-10-22

Таблица создана для импорта ЕМИАС (FR-25): emias_id NOT NULL UNIQUE, все строки
импортированные. FR-23 добавляет сюда же назначения врача:
  * source — 'emias' | 'clinician' (backfill существующих строк в 'emias');
  * clinician_id — автор назначения (NULL у импортированных);
  * ended_on — дата окончания приёма (в т.ч. при остановке);
  * notes — заметка врача (bytea, EncryptedText);
  * emias_id становится nullable, уникальность — partial unique index
    (WHERE emias_id IS NOT NULL), иначе все локальные NULL конфликтовали бы.

Миграция data-safe: существующие строки получают source='emias' (по emias_id).
Downgrade удаляет локальные назначения (в старой схеме их быть не может:
emias_id NOT NULL), затем снимает добавленные колонки.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f4a5b6c7d8e9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    # server_default — чтобы backfill не оставил NULL при существующих строках.
    op.add_column(
        "patient_medications",
        sa.Column("source", sa.String(length=16), nullable=False, server_default=sa.text("'clinician'")),
    )
    op.add_column("patient_medications", sa.Column("clinician_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "patient_medications_clinician_id_fkey",
        "patient_medications",
        "users",
        ["clinician_id"],
        ["id"],
    )
    op.add_column("patient_medications", sa.Column("ended_on", sa.Date(), nullable=True))
    # bytea — EncryptedText (pgp_sym_encrypt), как у title/dosage.
    op.add_column("patient_medications", sa.Column("notes", sa.LargeBinary(), nullable=True))

    # эмиас-строки: было emias_id NOT NULL UNIQUE → ограничение снимаем,
    # уникальность оставляем частичным индексом.
    op.drop_constraint("patient_medications_emias_id_key", "patient_medications", type_="unique")
    op.alter_column("patient_medications", "emias_id", existing_type=sa.String(length=64), nullable=True)
    op.create_index(
        "uq_patient_medications_emias_id",
        "patient_medications",
        ["emias_id"],
        unique=True,
        postgresql_where=sa.text("emias_id is not null"),
    )

    op.create_check_constraint(
        "patient_medications_source", "patient_medications", "source in ('emias','clinician')"
    )
    op.create_check_constraint(
        "patient_medications_dates",
        "patient_medications",
        "ended_on is null or started_on is null or ended_on >= started_on",
    )

    # Backfill: всё, что пришло из ЕМИАС, помечаем источником импорта.
    op.execute("UPDATE patient_medications SET source = 'emias' WHERE emias_id IS NOT NULL")

def downgrade() -> None:
    # Локальные назначения не представимы в старой схеме (emias_id NOT NULL):
    # удаляем их, иначе downgrade упадёт на NOT NULL.
    op.execute("DELETE FROM patient_medications WHERE emias_id IS NULL")
    op.drop_constraint("patient_medications_dates", "patient_medications", type_="check")
    op.drop_constraint("patient_medications_source", "patient_medications", type_="check")
    op.drop_index("uq_patient_medications_emias_id", table_name="patient_medications")
    op.alter_column("patient_medications", "emias_id", existing_type=sa.String(length=64), nullable=False)
    op.create_unique_constraint("patient_medications_emias_id_key", "patient_medications", ["emias_id"])
    op.drop_column("patient_medications", "notes")
    op.drop_column("patient_medications", "ended_on")
    op.drop_constraint("patient_medications_clinician_id_fkey", "patient_medications", type_="foreignkey")
    op.drop_column("patient_medications", "clinician_id")
    op.drop_column("patient_medications", "source")
