"""Дневник самочувствия (FR-9). Заметки — специальная категория ПДн (152-ФЗ)."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import Problem
from app.models import DiaryEntry


def create(db: Session, patient_id, content: str, mood: int | None = None, sleep_h: float | None = None, tags: Sequence[str] = ()) -> DiaryEntry:
    if mood is not None and not 0 <= mood <= 4:
        raise Problem(422, "VALIDATION", "mood должен быть в диапазоне 0..4")
    if sleep_h is not None and not 0 <= sleep_h <= 12:
        raise Problem(422, "VALIDATION", "sleep_h должен быть в диапазоне 0..12")
    entry = DiaryEntry(patient_id=patient_id, content=content, mood=mood, sleep_h=sleep_h, tags=list(tags))
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def list_for_patient(db: Session, patient_id, limit: int = 50) -> list[DiaryEntry]:
    rows = db.scalars(
        select(DiaryEntry).where(DiaryEntry.patient_id == patient_id).order_by(DiaryEntry.created_at.desc()).limit(limit)
    ).all()
    return list(rows)


def to_dict(entry: DiaryEntry) -> dict:
    return {
        "id": str(entry.id),
        "content": entry.content,
        "mood": entry.mood,
        "sleep_h": entry.sleep_h,
        "tags": entry.tags,
        "created_at": entry.created_at.isoformat(),
    }