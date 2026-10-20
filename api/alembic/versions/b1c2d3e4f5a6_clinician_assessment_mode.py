"""runs_mode: режим CLINICIAN для врачебных шкал (HDRS-17)

Revision ID: b1c2d3e4f5a6
Revises: e9c0d1e2f3a4
Create Date: 2026-10-20

Врачебная оценка — отдельный Run(mode='CLINICIAN'): пациент такие шкалы не
заполняет, поэтому режим не пересекается со SCREENING/CHECKIN и не ломает
runs_screening_open (партиальный индекс только по mode='SCREENING').
"""

from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("runs_mode", "runs", type_="check")
    op.create_check_constraint(
        "runs_mode", "runs", "mode in ('SCREENING','CHECKIN','FOLLOW_UP','CLINICIAN')"
    )


def downgrade() -> None:
    # Врачебные прогоны несовместимы со старым CHECK: удаляем их вместе с
    # зависимыми строками (FK без ON DELETE CASCADE).
    clinician_runs = "SELECT id FROM runs WHERE mode = 'CLINICIAN'"
    clinician_assessments = f"SELECT id FROM assessments WHERE run_id IN ({clinician_runs})"
    op.execute(
        "DELETE FROM notifications WHERE escalation_id IN "
        f"(SELECT id FROM escalations WHERE assessment_id IN ({clinician_assessments}))"
    )
    op.execute(
        "DELETE FROM escalations WHERE assessment_id IN "
        f"({clinician_assessments})"
    )
    op.execute(f"DELETE FROM assessments WHERE run_id IN ({clinician_runs})")
    op.execute(f"DELETE FROM answers WHERE run_id IN ({clinician_runs})")
    op.execute("DELETE FROM runs WHERE mode = 'CLINICIAN'")
    op.drop_constraint("runs_mode", "runs", type_="check")
    op.create_check_constraint(
        "runs_mode", "runs", "mode in ('SCREENING','CHECKIN','FOLLOW_UP')"
    )
