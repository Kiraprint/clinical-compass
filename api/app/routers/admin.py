# E3: admin/audit. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services
from app.db import get_db
from app.deps import require_admin
from app.errors import Problem
from app.models import AuditLog, ClinicianRoster, User

router = APIRouter(prefix="/admin", tags=["admin"])


class RosterBody(BaseModel):
    patient_id: uuid.UUID
    clinician_id: uuid.UUID


@router.post("/roster", status_code=201)
def add_roster(body: RosterBody, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    patient = db.get(User, body.patient_id)
    clinician = db.get(User, body.clinician_id)
    if patient is None or clinician is None:
        raise Problem(404, "NOT_FOUND", "Пользователь не найден")
    if patient.role != "PATIENT" or clinician.role != "CLINICIAN":
        raise Problem(422, "VALIDATION", "Роли: patient_id → PATIENT, clinician_id → CLINICIAN")
    if db.get(ClinicianRoster, (body.clinician_id, body.patient_id)) is not None:
        raise Problem(409, "CONFLICT", "Пациент уже закреплён за этим врачом")
    db.add(ClinicianRoster(clinician_id=body.clinician_id, patient_id=body.patient_id))
    services.audit.record(db, user.id, "ROSTER_GRANT", "clinician_roster", f"{body.clinician_id}:{body.patient_id}")
    db.commit()
    return {}


@router.get("/audit")
def list_audit(
    request: Request,
    after: datetime | None = Query(default=None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """F6: чтение журнала аудита само аудируется (доступ к истории спецкатегории)."""
    ip = request.client.host if request.client else None
    services.audit.record(db, user.id, "READ_AUDIT", "audit_log", ip=ip)
    db.commit()
    q = select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(500)
    if after is not None:
        q = q.where(AuditLog.occurred_at >= after)
    rows = db.scalars(q).all()
    return {
        "items": [
            {
                "id": r.id,
                "actor_id": str(r.actor_id) if r.actor_id else None,
                "action": r.action,
                "object_type": r.object_type,
                "object_id": r.object_id,
                "ip": r.ip,
                "occurred_at": r.occurred_at.isoformat(),
            }
            for r in rows
        ]
    }