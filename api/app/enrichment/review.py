"""Ревью источников: approve/reject статьи, публикация в индекс, аудит.

Апрув — единственный путь статьи в Qdrant. Отклонение хранит причину и не
удаляет запись: повторный прогон не «воскрешает» отклонённый PMID как новый
кандидат (он обновится, но статус rejected останется, если версия та же).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enrichment import criteria as criteria_mod
from app.enrichment import index as index_mod
from app.enrichment.config import EnrichmentConfig
from app.models import KBExclusion, KBWork
from app.services import audit
from app.services.embeddings import build_embedder


class ReviewError(ValueError):
    """Недопустимый переход статуса (например, approve уже отклонённого)."""


def get_work(db: Session, work_id: uuid.UUID, *, for_update: bool = False) -> KBWork | None:
    # for_update: два админа (или админ + прогон) не должны решать судьбу одной
    # записи параллельно — иначе approve и reject перетирают друг друга.
    stmt = select(KBWork).where(KBWork.id == work_id)
    if for_update:
        # Явный FOR UPDATE: db.get() отдаёт объект из identity map, и блокировка
        # строки может не примениться, если объект уже загружен в сессию.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return db.scalar(stmt)


def get_work_by_guideline(db: Session, guideline_id: str) -> KBWork | None:
    return db.scalar(select(KBWork).where(KBWork.guideline_id == guideline_id))


def list_works(
    db: Session,
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[KBWork]:
    query = select(KBWork).order_by(KBWork.last_seen_at.desc()).offset(offset).limit(limit)
    if status:
        query = query.where(KBWork.status == status)
    if kind:
        query = query.where(KBWork.kind == kind)
    return list(db.scalars(query))


def approve(
    db: Session,
    client,
    work_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    cfg: EnrichmentConfig,
    *,
    embedder=None,
) -> KBWork:
    work = get_work(db, work_id, for_update=True)
    if work is None:
        raise ReviewError("Источник не найден.")
    if work.status != "candidate":
        raise ReviewError(
            f"Approve допустим только для candidate (сейчас {work.status})."
        )
    # Approve — не обход методологии: подтверждённые нарушения жёстких
    # критериев (ретракция, проприетарная лицензия, не психиатрия) блокируют
    # публикацию до появления исправленной версии.
    hard = criteria_mod.GUIDELINE_HARD if work.kind == "guideline" else criteria_mod.ARTICLE_HARD
    blocking = [
        verdict
        for verdict in (work.criteria or {}).get("verdicts", [])
        if verdict.get("key") in hard and verdict.get("status") == "not_met"
    ]
    if blocking:
        labels = "; ".join(
            f"{item.get('label')}: {item.get('explanation')}" for item in blocking
        )
        raise ReviewError(f"Жёсткие критерии не выполнены, approve запрещён. {labels}")
    embedder = embedder or build_embedder()
    now = dt.datetime.now(dt.timezone.utc)
    # Статус фиксируется ДО индексации: точка в Qdrant не может существовать
    # для работы, которая в реестре всё ещё candidate (сбой финального commit
    # в вызывающем коде иначе оставлял бы неодобренный контент в выдаче).
    work.status = "approved"
    work.chunk_count = 0
    work.indexed_hash = None
    work.published_at = None
    work.approved_by = actor_id
    work.approved_at = now
    work.reject_reason = None
    db.commit()
    try:
        chunks = index_mod.index_work(
            client, embedder, work, chunk_chars=cfg.chunk_chars, chunk_overlap=cfg.chunk_overlap
        )
    except Exception:
        # Индексация не удалась: точки (если успели записаться) снимаем, работу
        # возвращаем в очередь — но только если параллельное решение её не
        # успело изменить (иначе затрём reject/новую версию).
        try:
            index_mod.remove_work(client, work)
        except Exception:  # noqa: BLE001 — уборка не должна маскировать исходную ошибку
            pass
        current = get_work(db, work.id, for_update=True)
        if (
            current is not None
            and current.status == "approved"
            and current.content_hash == work.content_hash
        ):
            current.status = "candidate"
            current.approved_by = None
            current.approved_at = None
            current.published_at = None
            current.chunk_count = 0
            current.indexed_hash = None
            db.commit()
        else:
            db.rollback()
        raise
    # Индексация шла без блокировки строки: подтверждаем под блокировкой, что
    # работа всё ещё approved и той же версии, иначе точки принадлежат уже
    # отклонённой/замещённой версии и индекс надо очистить.
    current = get_work(db, work.id, for_update=True)
    if (
        current is None
        or current.status != "approved"
        or current.content_hash != work.content_hash
    ):
        try:
            index_mod.remove_work_id(client, work.id)
        except Exception:  # noqa: BLE001
            pass
        db.rollback()
        raise ReviewError("Работа изменена параллельным решением; индексация отменена.")
    current.chunk_count = chunks
    current.indexed_hash = current.content_hash
    current.published_at = now
    audit.record(db, actor_id, "KB_SOURCE_APPROVE", "kb_work", str(current.id))
    db.commit()
    return current


def reject(
    db: Session,
    work_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    reason: str,
    *,
    client=None,
) -> KBWork:
    work = get_work(db, work_id, for_update=True)
    if work is None:
        raise ReviewError("Источник не найден.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ReviewError("Нужна причина отклонения (≥ 3 символов).")
    if work.status == "approved" and client is not None:
        index_mod.remove_work(client, work)
        work.indexed_hash = None
        work.chunk_count = 0
    work.status = "rejected"
    work.reject_reason = reason[:2000]
    work.approved_by = actor_id
    work.approved_at = dt.datetime.now(dt.timezone.utc)
    audit.record(db, actor_id, "KB_SOURCE_REJECT", "kb_work", str(work.id))
    db.flush()
    return work


def excluded_ids(db: Session, user_id: uuid.UUID) -> set[str]:
    """guideline_id, которые врач скрыл персонально (фильтр retrieval)."""
    rows = db.scalars(select(KBExclusion.guideline_id).where(KBExclusion.user_id == user_id))
    return set(rows)


def set_exclusion(
    db: Session, user_id: uuid.UUID, guideline_id: str, excluded: bool
) -> list[str]:
    existing = db.scalar(
        select(KBExclusion).where(
            KBExclusion.user_id == user_id, KBExclusion.guideline_id == guideline_id
        )
    )
    if excluded and existing is None:
        db.add(KBExclusion(user_id=user_id, guideline_id=guideline_id))
    elif not excluded and existing is not None:
        db.delete(existing)
    audit.record(
        db,
        user_id,
        "KB_SOURCE_EXCLUDE" if excluded else "KB_SOURCE_INCLUDE",
        "kb_guideline",
        guideline_id,
    )
    db.flush()
    return sorted(excluded_ids(db, user_id))


def serialize(work: KBWork, *, excluded: bool = False) -> dict:
    """Представление для API. Полный текст не отдаём никогда (объём/лицензия)."""
    return {
        "id": str(work.id),
        "source": work.source,
        "external_id": work.external_id,
        "guideline_id": work.guideline_id,
        "kind": work.kind,
        "status": work.status,
        "title": work.title,
        "revision": work.revision,
        "year": (work.meta or {}).get("year"),
        "doi": (work.meta or {}).get("doi"),
        "pmid": (work.meta or {}).get("pmid"),
        "url": (work.meta or {}).get("url"),
        "fulltext_url": (work.meta or {}).get("fulltext_url"),
        "license": (work.meta or {}).get("license"),
        "journal": (work.meta or {}).get("journal"),
        "abstract": (work.meta or {}).get("abstract"),
        "results": (work.meta or {}).get("results"),
        "publication_types": (work.meta or {}).get("publication_types") or [],
        "retraction": work.retraction,
        "design": work.design,
        "criteria": work.criteria,
        "appraisal": work.appraisal,
        "chunk_count": work.chunk_count,
        "indexed": bool(work.indexed_hash),
        "first_seen_at": work.first_seen_at.isoformat() if work.first_seen_at else None,
        "last_seen_at": work.last_seen_at.isoformat() if work.last_seen_at else None,
        "published_at": work.published_at.isoformat() if work.published_at else None,
        "approved_at": work.approved_at.isoformat() if work.approved_at else None,
        "reject_reason": work.reject_reason,
        "excluded": excluded,
    }
