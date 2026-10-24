"""FR-27: «не назначено сегодня» и уточнение частичного приёма

Revision ID: d1a2b3c4e5f6
Revises: c8d9e0f1a2b3
Create Date: 2026-10-24

Обратная связь врача-психиатра по отметкам приёма:
  * status расширяется значением not_scheduled («сегодня не назначено»:
    перерыв, приём по требованию). Такая отметка не входит ни в числитель,
    ни в знаменатель процента — это не пропуск.
  * partial_kind — уточнение «частично»: quarter | half | three_quarters |
    one_of_two | two_of_three | less_than_prescribed | self_changed.
    CHECK: kind только при status='partial' и обязателен для него.

Backfill: существующие partial не имели уточнения. Прежняя семантика
«частично» = принял меньше назначенного, поэтому им проставляется
less_than_prescribed (доля 0.5 — ровно как считалось раньше), чтобы
инвариант «kind обязателен для partial» держался на уровне БД.

Обратимость: downgrade проверяет, что все partial имеют kind (иначе
откат неоднозначен), обнуляет partial_kind, удаляет колонку и возвращает
исходный домен status. Отметки не удаляются: строки not_scheduled не
представимы старым доменом, поэтому старый CHECK добавляется как NOT VALID
(проверяет только новые записи) — история пациента сохраняется.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1a2b3c4e5f6"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None

STATUSES = "('taken','partial','missed','not_scheduled')"
PARTIAL_KINDS = (
    "('quarter','half','three_quarters','one_of_two','two_of_three',"
    "'less_than_prescribed','self_changed')"
)


def upgrade() -> None:
    op.drop_constraint("adherence_records_status", "adherence_records", type_="check")
    op.add_column(
        "adherence_records",
        sa.Column("partial_kind", sa.String(32), nullable=True),
    )
    op.execute(
        "UPDATE adherence_records SET partial_kind = 'less_than_prescribed' "
        "WHERE status = 'partial'"
    )
    op.create_check_constraint(
        "adherence_records_status", "adherence_records", f"status in {STATUSES}",
    )
    op.create_check_constraint(
        "adherence_records_partial_kind", "adherence_records",
        f"partial_kind is null or partial_kind in {PARTIAL_KINDS}",
    )
    op.create_check_constraint(
        "adherence_records_partial_kind_required", "adherence_records",
        "(status = 'partial') = (partial_kind is not null)",
    )


def downgrade() -> None:
    # Откат без потери данных: partial-отметки без kind невозможны по
    # констрейнту; если он всё же нарушен руками — останавливаемся.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM adherence_records
                WHERE status = 'partial' AND partial_kind IS NULL
            ) THEN
                RAISE EXCEPTION
                    'downgrade d1a2b3c4e5f6: partial без partial_kind — откат неоднозначен';
            END IF;
        END
        $$;
        """
    )
    op.drop_constraint(
        "adherence_records_partial_kind_required", "adherence_records", type_="check",
    )
    op.drop_constraint(
        "adherence_records_partial_kind", "adherence_records", type_="check",
    )
    # Старая схема не знает уточнения: заполняем NULL и убираем колонку.
    op.execute("UPDATE adherence_records SET partial_kind = NULL WHERE status = 'partial'")
    op.drop_column("adherence_records", "partial_kind")
    op.drop_constraint("adherence_records_status", "adherence_records", type_="check")
    # NOT VALID: сохраняем строки not_scheduled, но запрещаем их для новых
    # записей (старое приложение таких статусов не пишет).
    op.execute(
        "ALTER TABLE adherence_records ADD CONSTRAINT adherence_records_status "
        f"CHECK (status in ('taken','partial','missed')) NOT VALID"
    )
