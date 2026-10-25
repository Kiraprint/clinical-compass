# E4: screenings. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.db import get_db
from app.deps import require_consent
from app.models import User

router = APIRouter(tags=["screenings"])


class ScreeningBody(BaseModel):
    """Шкала — опциональна: пациент выбирает из назначенных врачом (item 1, multi-scale).
    Без questionnaire_code — первичная назначенная. Произвольная шкала → 403 SCALE_NOT_PRESCRIBED."""
    questionnaire_code: str | None = None


class AnswersBody(BaseModel):
    answers: dict[str, int]


@router.get("/questionnaires")
async def list_questionnaires(user: User = Depends(require_consent), db: AsyncSession = Depends(get_db)):
    """Каталог шкал для пациента (FR-2). Единый источник текстов — core/compass/scales.py.

    Клиницистские шкалы (HDRS-17) пациенту недоступны: их заполняет только врач.
    """
    from compass.scales import patient_administered_scales

    catalog = []
    for s in patient_administered_scales():
        items = s.patient_items()
        catalog.append({
            "code": s.code,
            "title": s.title,
            # Сумма максимумов предъявленных пунктов: у PHQ-9 (пациентская PHQ-8) 24,
            # у GAD-7 21, у HCL-32 32, у PSS-10 40 — график тренда берёт верх шкалы отсюда,
            # а не угадывает его по коду шкалы.
            "max_total": sum(i.max_value for i in items),
            "items": [
                {"key": i.key, "text": i.text, "max_value": i.max_value, "anchors": list(i.anchors)}
                for i in items
            ],
        })
    return catalog


@router.get("/me/prescribed-scales")
async def my_prescribed_scales(request: Request, user: User = Depends(require_consent), db: AsyncSession = Depends(get_db)):
    """Шкалы, назначенные пациенту врачом (item 1, multi-scale). Без назначения — дефолт [PHQ-9].

    F6: чтение назначенных шкал раскрывает контекст лечения — аудируется.
    """
    from compass.scales import ALL_SCALES

    ip = request.client.host if request.client else None
    await services.audit.record(db, user.id, "READ_PRESCRIBED_SCALES", "patient", ip=ip)
    await db.commit()
    # completed_at — есть ли по этому назначению завершённая оценка; главный экран
    # прячет баннер «Назначен скрининг», когда шкала уже пройдена (FR-33).
    rows = await services.screening.prescription_status(db, user.id)
    return {
        "scales": [
            {
                "code": r["code"],
                "title": ALL_SCALES[r["code"]].title,
                "prescribed_at": r["prescribed_at"].isoformat() if r["prescribed_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in rows
        ]
    }

@router.post("/screenings", status_code=201)
async def create_screening(body: ScreeningBody, user: User = Depends(require_consent), db: AsyncSession = Depends(get_db)):
    run = await services.screening.start_screening(db, user.id, body.questionnaire_code)
    return {
        "run_id": str(run.id),
        "questionnaire_code": run.questionnaire_code,
        "mode": run.mode,
        "started_at": run.started_at.isoformat(),
    }


@router.post("/runs/{run_id}/answers", status_code=204)
async def submit_answers(
    run_id: uuid.UUID, body: AnswersBody, user: User = Depends(require_consent), db: AsyncSession = Depends(get_db)
):
    await services.screening.add_answers(db, run_id, user.id, body.answers)


@router.post("/runs/{run_id}/finish", status_code=201)
async def finish_run(run_id: uuid.UUID, user: User = Depends(require_consent), db: AsyncSession = Depends(get_db)):
    return await services.screening.finish_screening(db, run_id, user.id)

@router.post("/screenings/abandon", status_code=200)
async def abandon_open(user: User = Depends(require_consent), db: AsyncSession = Depends(get_db)):
    """Закрывает брошенный открытый скрининг пациента (иначе новый /screenings → 409)."""
    return await services.screening.abandon_open(db, user.id)