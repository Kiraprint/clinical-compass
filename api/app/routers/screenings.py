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

    return [
        {
            "code": s.code,
            "title": s.title,
            "items": [
                {"key": i.key, "text": i.text, "max_value": i.max_value, "anchors": list(i.anchors)}
                for i in s.patient_items()
            ],
        }
        for s in patient_administered_scales()
    ]


@router.get("/me/prescribed-scales")
async def my_prescribed_scales(request: Request, user: User = Depends(require_consent), db: AsyncSession = Depends(get_db)):
    """Шкалы, назначенные пациенту врачом (item 1, multi-scale). Без назначения — дефолт [PHQ-9].

    F6: чтение назначенных шкал раскрывает контекст лечения — аудируется.
    """
    from compass.scales import ALL_SCALES

    ip = request.client.host if request.client else None
    await services.audit.record(db, user.id, "READ_PRESCRIBED_SCALES", "patient", ip=ip)
    await db.commit()
    codes = await services.screening.prescribed_scales(db, user.id)
    return {"scales": [{"code": c, "title": ALL_SCALES[c].title} for c in codes]}

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