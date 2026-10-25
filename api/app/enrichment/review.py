"""Ревью источников: approve/reject статьи, публикация в индекс, аудит.

Апрув — единственный путь статьи в Qdrant. Отклонение хранит причину и не
удаляет запись: повторный прогон не «воскрешает» отклонённый PMID как новый
кандидат (он обновится, но статус rejected останется, если версия та же).

FR-36: админский approve — публикация в индекс (глобально), а врач решает
персонально, видит ли ОН эту статью (kb_work_approvals, docs/15 §12).
"""

from __future__ import annotations

import anyio

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enrichment import criteria as criteria_mod
from app.enrichment import index as index_mod
from app.enrichment.config import EnrichmentConfig
from app.models import KBExclusion, KBWork, KBWorkApproval
from app.services import audit
from app.services.embeddings import get_embedder


class ReviewError(ValueError):
    """Недопустимый переход статуса (например, approve уже отклонённого)."""


async def get_work(db: AsyncSession, work_id: uuid.UUID, *, for_update: bool = False) -> KBWork | None:
    # for_update: два админа (или админ + прогон) не должны решать судьбу одной
    # записи параллельно — иначе approve и reject перетирают друг друга.
    stmt = select(KBWork).where(KBWork.id == work_id)
    if for_update:
        # Явный FOR UPDATE: db.get() отдаёт объект из identity map, и блокировка
        # строки может не примениться, если объект уже загружен в сессию.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(stmt)


async def get_work_by_guideline(db: AsyncSession, guideline_id: str) -> KBWork | None:
    return await db.scalar(select(KBWork).where(KBWork.guideline_id == guideline_id))


async def list_works(
    db: AsyncSession,
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
    return list(await db.scalars(query))


async def approve(
    db: AsyncSession,
    client,
    work_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    cfg: EnrichmentConfig,
    *,
    embedder=None,
) -> KBWork:
    work = await get_work(db, work_id, for_update=True)
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
    embedder = embedder or get_embedder()
    now = dt.datetime.now(dt.timezone.utc)
    # Хэш версии снимается в значение: после индексации объект перечитывается
    # под блокировкой (populate_existing обновляет тот же instance), и сравнение
    # content_hash с самим собой не поймало бы подмену версии параллельным прогоном.
    version_hash = work.content_hash
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
    await db.commit()
    try:
        chunks = await anyio.to_thread.run_sync(
            lambda: index_mod.index_work(
                client, embedder, work,
                chunk_chars=cfg.chunk_chars, chunk_overlap=cfg.chunk_overlap,
            )
        )
    except Exception:
        # Индексация не удалась: точки (если успели записаться) снимаем, работу
        # возвращаем в очередь — но только если параллельное решение её не
        # успело изменить (иначе затрём reject/новую версию).
        try:
            await anyio.to_thread.run_sync(index_mod.remove_work, client, work)
        except Exception:  # noqa: BLE001 — уборка не должна маскировать исходную ошибку
            pass
        current = await get_work(db, work.id, for_update=True)
        if (
            current is not None
            and current.status == "approved"
            and current.content_hash == version_hash
        ):
            current.status = "candidate"
            current.approved_by = None
            current.approved_at = None
            current.published_at = None
            current.chunk_count = 0
            current.indexed_hash = None
            await db.commit()
        else:
            await db.rollback()
        raise
    # Индексация шла без блокировки строки: подтверждаем под блокировкой, что
    # работа всё ещё approved и той же версии, иначе точки принадлежат уже
    # отклонённой/замещённой версии и индекс надо очистить.
    current = await get_work(db, work.id, for_update=True)
    if (
        current is None
        or current.status != "approved"
        or current.content_hash != version_hash
    ):
        try:
            await anyio.to_thread.run_sync(index_mod.remove_work_id, client, work.id)
        except Exception:  # noqa: BLE001
            pass
        await db.rollback()
        raise ReviewError("Работа изменена параллельным решением; индексация отменена.")
    current.chunk_count = chunks
    current.indexed_hash = current.content_hash
    current.published_at = now
    await audit.record(db, actor_id, "KB_SOURCE_APPROVE", "kb_work", str(current.id))
    await db.commit()
    return current


async def reject(
    db: AsyncSession,
    work_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    reason: str,
    *,
    client=None,
) -> KBWork:
    work = await get_work(db, work_id, for_update=True)
    if work is None:
        raise ReviewError("Источник не найден.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ReviewError("Нужна причина отклонения (≥ 3 символов).")
    if work.status == "approved" and client is not None:
        await anyio.to_thread.run_sync(index_mod.remove_work, client, work)
        work.indexed_hash = None
        work.chunk_count = 0
    work.status = "rejected"
    work.reject_reason = reason[:2000]
    work.approved_by = actor_id
    work.approved_at = dt.datetime.now(dt.timezone.utc)
    await audit.record(db, actor_id, "KB_SOURCE_REJECT", "kb_work", str(work.id))
    await db.flush()
    return work


async def excluded_ids(db: AsyncSession, user_id: uuid.UUID) -> set[str]:
    """guideline_id, которые врач скрыл персонально (фильтр retrieval)."""
    rows = await db.scalars(select(KBExclusion.guideline_id).where(KBExclusion.user_id == user_id))
    return set(rows)


async def set_exclusion(
    db: AsyncSession, user_id: uuid.UUID, guideline_id: str, excluded: bool
) -> list[str]:
    existing = await db.scalar(
        select(KBExclusion).where(
            KBExclusion.user_id == user_id, KBExclusion.guideline_id == guideline_id
        )
    )
    if excluded and existing is None:
        db.add(KBExclusion(user_id=user_id, guideline_id=guideline_id))
    elif not excluded and existing is not None:
        await db.delete(existing)
    await audit.record(
        db,
        user_id,
        "KB_SOURCE_EXCLUDE" if excluded else "KB_SOURCE_INCLUDE",
        "kb_guideline",
        guideline_id,
    )
    await db.flush()
    return sorted(await excluded_ids(db, user_id))


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


# --- FR-36: персональное ревью статьи врачом (docs/15 §12) ------------------

class ApprovalError(ValueError):
    """Решение врача невозможно; `code` — машинный код для HTTP-слоя."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def approved_work_ids(db: AsyncSession, clinician_id: uuid.UUID) -> set[str]:
    """work_id статей, которые этот врач одобрил лично (allow-list retrieval).

    Гайдлайны ВОЗ сюда не входят намеренно: они видны всем и без решения
    (фильтр в retrieval пропускает kind='guideline' и seed-чанки без work_id).
    """
    rows = await db.scalars(
        select(KBWorkApproval.work_id).where(
            KBWorkApproval.clinician_id == clinician_id,
            KBWorkApproval.decision == "approved",
        )
    )
    return {str(work_id) for work_id in rows}


async def list_clinician_works(
    db: AsyncSession,
    clinician_id: uuid.UUID,
    *,
    status: str = "new",
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple[KBWork, KBWorkApproval | None]]:
    """Опубликованные работы + моё решение по ним (FR-36, docs/04 FR-36).

    База — только опубликованное админом (`status='approved'`): неопубликованную
    статью врач решать не может, и в его библиотеке её быть не должно.
    `new` — работы без моей записи; `approved`/`rejected` — по моему решению;
    `all` — все опубликованные.
    """
    stmt = (
        select(KBWork, KBWorkApproval)
        .outerjoin(
            KBWorkApproval,
            (KBWorkApproval.work_id == KBWork.id)
            & (KBWorkApproval.clinician_id == clinician_id),
        )
        .where(KBWork.status == "approved")
        .order_by(KBWork.published_at.desc().nullslast(), KBWork.last_seen_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if kind:
        stmt = stmt.where(KBWork.kind == kind)
    if status == "new":
        stmt = stmt.where(KBWorkApproval.id.is_(None))
    elif status in ("approved", "rejected"):
        stmt = stmt.where(KBWorkApproval.decision == status)
    rows = (await db.execute(stmt)).all()
    return [(work, approval) for work, approval in rows]


def serialize_for_clinician(work: KBWork, approval: KBWorkApproval | None) -> dict:
    """Карточка статьи для экрана «Библиотека»: без полного текста/абстракта.

    `criteria` сжато: решение, релевантность и вердикты без цитат-обоснований —
    экрану нужен светофор критериев, а не админская карточка ревью.
    """
    meta = work.meta or {}
    criteria = work.criteria or {}
    return {
        "work_id": str(work.id),
        "title": work.title,
        "source": work.source,
        "year": meta.get("year"),
        "source_url": meta.get("url") or "",
        # Полный текст (PMC для open-access статей, карточка IRIS для ВОЗ):
        # в индексе лежит только аннотация, поэтому ссылку отдаём врачу.
        "fulltext_url": meta.get("fulltext_url") or "",
        "design": work.design or {},
        "criteria": {
            "decision": criteria.get("decision"),
            "relevance_score": criteria.get("relevance_score"),
            "verdicts": [
                {
                    "key": verdict.get("key"),
                    "label": verdict.get("label"),
                    "status": verdict.get("status"),
                }
                for verdict in criteria.get("verdicts") or []
            ],
        },
        "decision": approval.decision if approval else None,
        "reviewed_at": approval.reviewed_at.isoformat() if approval else None,
    }


async def set_decision(
    db: AsyncSession,
    clinician_id: uuid.UUID,
    work_id: uuid.UUID,
    decision: str,
    *,
    note: str | None = None,
) -> tuple[KBWork, KBWorkApproval]:
    """Одобрить/отклонить статью лично для себя (идемпотентно).

    Повтор того же решения — 200 без побочных эффектов: reviewed_at и аудит
    не трогаем (иначе повторный клик «Одобрить» переписывал бы историю).
    Смена решения обновляет строку и reviewed_at; аудит `KB_WORK_REVIEW` несёт
    только work_id — свободный текст `note` не пишем ни в БД, ни в журнал
    (152-ФЗ: заметка врача может содержать ПДн; параметр принят для контракта
    с UI, хранение пока не требуется продуктом).
    """
    work = await get_work(db, work_id, for_update=True)
    if work is None:
        raise ApprovalError("KB_WORK_NOT_FOUND", "Источник не найден.")
    if work.status != "approved":
        raise ApprovalError(
            "KB_WORK_NOT_PUBLISHED",
            "Статья ещё не опубликована администратором — решение недоступно.",
        )
    approval = await db.scalar(
        select(KBWorkApproval)
        .where(
            KBWorkApproval.clinician_id == clinician_id,
            KBWorkApproval.work_id == work.id,
        )
        .with_for_update()
    )
    changed = False
    now = dt.datetime.now(dt.timezone.utc)
    if approval is None:
        approval = KBWorkApproval(
            clinician_id=clinician_id,
            work_id=work.id,
            decision=decision,
            # Явное значение, а не только server_default: ответ на POST должен
            # нести reviewed_at без дополнительного refresh.
            reviewed_at=now,
        )
        db.add(approval)
        changed = True
    elif approval.decision != decision:
        approval.decision = decision
        approval.reviewed_at = now
        changed = True
    if changed:
        await audit.record(db, clinician_id, "KB_WORK_REVIEW", "kb_work", str(work.id))
    await db.flush()
    return work, approval
