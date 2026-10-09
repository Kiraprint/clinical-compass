# E4: screenings. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import services
from app.db import get_db
from app.deps import require_consent
from app.models import User

router = APIRouter(tags=["screenings"])


class ScreeningBody(BaseModel):
    questionnaire_code: str = Field(min_length=1)


class AnswersBody(BaseModel):
    answers: dict[str, int]


@router.get("/questionnaires")
def list_questionnaires(user: User = Depends(require_consent), db: Session = Depends(get_db)):
    """Каталог шкал (FR-2). Единый источник текстов — core/compass/scales.py."""
    from compass.scales import ALL_SCALES

    return [
        {
            "code": s.code,
            "title": s.title,
            "items": [{"key": i.key, "text": i.text, "max_value": i.max_value} for i in s.items],
        }
        for s in ALL_SCALES.values()
    ]


@router.post("/screenings", status_code=201)
def create_screening(body: ScreeningBody, user: User = Depends(require_consent), db: Session = Depends(get_db)):
    run = services.screening.start_screening(db, user.id, body.questionnaire_code)
    return {
        "run_id": str(run.id),
        "questionnaire_code": run.questionnaire_code,
        "mode": run.mode,
        "started_at": run.started_at.isoformat(),
    }


@router.post("/runs/{run_id}/answers", status_code=204)
def submit_answers(
    run_id: uuid.UUID, body: AnswersBody, user: User = Depends(require_consent), db: Session = Depends(get_db)
):
    services.screening.add_answers(db, run_id, user.id, body.answers)


@router.post("/runs/{run_id}/finish", status_code=201)
def finish_run(run_id: uuid.UUID, user: User = Depends(require_consent), db: Session = Depends(get_db)):
    return services.screening.finish_screening(db, run_id, user.id)