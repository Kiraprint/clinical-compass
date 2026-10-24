"""Справочник препаратов (таблица drugs, источник — ГРЛС). Не ПДн.

Доступен врачу/админу для справки и автодополнения в формах (напр. suspected_drug
в НЯ). Не gated по consent — это публичные факты регистрационного удостоверения.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, require_clinician
from app.models import Drug, User

router = APIRouter(tags=["drugs"])


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
    _user: User = Depends(require_clinician),
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


@router.get("/drugs/{drug_id}")
async def get_drug(
    drug_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_clinician),
) -> dict:
    d = (await db.execute(select(Drug).where(Drug.drug_id == drug_id))).scalar_one_or_none()
    if d is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "detail": "Препарат не найден"})
    return _drug_dict(d)
