"""Справочник препаратов (таблица drugs, источник — ГРЛС). Не ПДн.

Доступен врачу/админу для справки и автодополнения в формах (напр. suspected_drug
в НЯ). Не gated по consent — это публичные факты регистрационного удостоверения.

Пациенту доступен только срез справочника по СВОИМ назначениям (`/me/drugs`):
полный каталог — не его данные, а название препарата раскрывает контекст лечения.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, require_consent, require_roles
from app.errors import Problem
from app.models import Drug, PatientMedication, User
from app.services.audit import record as audit_record

router = APIRouter(tags=["drugs"])

# Подписи статусов назначения — как в истории пациента (FR-23), без второго словаря.
from app.services.patient_history import MED_STATUS_LABELS  # noqa: E402


def _drug_dict(d: Drug) -> dict:
    return {
        "drug_id": d.drug_id,
        "trade_name": d.trade_name,
        "inn": d.inn,
        "inn_en": d.inn_en,
        "form": d.form,
        "reg_certificate": d.reg_certificate,
        "reg_date": d.reg_date.isoformat() if d.reg_date else None,
        "revision_date": d.revision_date.isoformat() if d.revision_date else None,
        "pharmacology": d.pharmacology,
        "indications": d.indications,
        "dosage": d.dosage,
        "contraindications": d.contraindications,
        "adverse_events": d.adverse_events,
        "special_notes": d.special_notes,
        "source_urls": d.source_urls or [],
    }


@router.get("/drugs")
async def list_drugs(
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_roles("CLINICIAN", "ADMIN")),
) -> dict:
    """Список препаратов. Опциональный фильтр q — ILIKE по trade_name/inn/inn_en."""
    stmt = select(Drug).order_by(Drug.drug_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Drug.trade_name.ilike(like))
            | (Drug.inn.ilike(like))
            | (Drug.inn_en.ilike(like))
        ).order_by(Drug.trade_name)
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [_drug_dict(d) for d in rows]}


def _as_key(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _match_drug(title: str, index: dict[str, Drug]) -> Drug | None:
    """Связь назначения с ГРЛС по названию/МНН (FK в patient_medications нет).

    Только точное совпадение нормализованной строки: подстрочный матч цеплял
    чужую карточку («Авиандр» в «Авиандр 150 мг» — угадывание), а порядок
    справочника делал результат недетерминированным. Нет совпадения — нет
    карточки: препарат не показываем, а не показываем похожий.
    """
    normalized = _as_key(title)
    if not normalized:
        return None
    return index.get(normalized)


@router.get("/me/drugs")
async def my_drug_reference(
    request: Request,
    user: User = Depends(require_consent), db: AsyncSession = Depends(get_db),
) -> dict:
    """Справочник по препаратам, назначенным пациенту (свои назначения + ГРЛС).

    F6: назначения и названия препаратов — контекст лечения, чтение аудируется.
    """
    ip = request.client.host if request.client else None
    await audit_record(db, user.id, "READ_MY_DRUGS", "patient", str(user.id), ip=ip)
    await db.commit()  # аудит чтения своего PHI сохраняется, даже если items пуст
    meds = (await db.scalars(
        select(PatientMedication)
        .where(PatientMedication.patient_id == user.id)
        .order_by(
            PatientMedication.started_on.desc().nullslast(),
            PatientMedication.created_at.desc(),
            PatientMedication.id.desc(),
        )
    )).all()
    if not meds:
        return {"items": []}
    drugs = (await db.scalars(select(Drug).order_by(Drug.drug_id))).all()
    index: dict[str, Drug] = {}
    for drug in drugs:
        for value in (drug.trade_name, drug.inn):
            key = _as_key(value)
            if key:
                index.setdefault(key, drug)
    return {
        "items": [
            {
                "medication_id": str(m.id),
                "title": m.title,
                "dosage": m.dosage,
                "status": m.status,
                "status_label": MED_STATUS_LABELS.get(m.status, m.status),
                "times_per_day": m.times_per_day,
                "drug": _drug_dict(drug) if (drug := _match_drug(m.title, index)) else None,
            }
            for m in meds
        ]
    }
@router.get("/drugs/{drug_id}")
async def get_drug(
    drug_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_roles("CLINICIAN", "ADMIN")),
) -> dict:
    d = (await db.execute(select(Drug).where(Drug.drug_id == drug_id))).scalar_one_or_none()
    if d is None:
        raise Problem(404, "NOT_FOUND", "Препарат не найден")
    return _drug_dict(d)
