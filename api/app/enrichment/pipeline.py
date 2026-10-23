"""Оркестрация прогона: источники → дедуп → критерии → оценка → реестр → индекс.

Последовательность (ТЗ п. 3): запрос/стратегия → публикации из подключённых
баз → дедупликация → критерии → дизайн → релевантность → оценка методологии →
статус публикации → итоговый список. Результаты не считаются фактическими,
пока источник не ответил: ошибка базы попадает в kb_runs.errors и делает
прогон partial/failed, а не «пустым результатом».

Гайдлайны ВОЗ публикуются автоматически (после детерминированных гейтов),
статьи ждут approve врача. Всё, что не approved, физически отсутствует в
Qdrant — агент не может это процитировать.
"""

from __future__ import annotations

import anyio

import datetime as dt
import hashlib
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enrichment import (
    APPRAISAL_VERSION,
    CRITERIA_VERSION,
    DESIGN_VERSION,
    ENRICHMENT_VERSION,
)
from app.enrichment import appraisal as appraisal_mod
from app.enrichment import criteria as criteria_mod
from app.enrichment import dedupe, design as design_mod, index as index_mod, pubmed, who
from app.enrichment.config import EnrichmentConfig
from app.enrichment.types import VERDICT_MET, SourceDocument
from app.models import KBRun, KBWork
from app.services.embeddings import build_embedder



@dataclass
class WorkDecision:
    source: str
    external_id: str
    kind: str
    title: str
    design: str
    decision: str
    status: str
    relevance_score: float
    reasons: tuple[str, ...] = ()
    appraisal_tool: str = "none"
    appraisal_summary: str = ""
    chunks: int = 0
    updated: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunSummary:
    run_id: str | None
    status: str
    stats: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    query_plan: dict = field(default_factory=dict)
    decisions: list[WorkDecision] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stats": self.stats,
            "errors": self.errors,
            "query_plan": self.query_plan,
            "decisions": [d.as_dict() for d in self.decisions],
        }


def _versions(cfg: EnrichmentConfig, embedder) -> dict:
    return {
        "enrichment": ENRICHMENT_VERSION,
        "criteria": CRITERIA_VERSION,
        "design": DESIGN_VERSION,
        "appraisal": APPRAISAL_VERSION,
        "chunker": {"chars": cfg.chunk_chars, "overlap": cfg.chunk_overlap},
        "embedder": {
            "name": type(embedder).__name__,
            "dense_size": getattr(embedder, "dense_size", None),
        },
        "pubmed_query": cfg.pubmed.query,
        "who_handles": [h.handle for h in cfg.who.handles],
    }


def _meta(doc: SourceDocument) -> dict:
    """JSONB-метаданные: то, что видят ревьюер и API, без полного текста."""
    return {
        "journal": doc.journal,
        "year": doc.year,
        "authors": list(doc.authors[:20]),
        "doi": dedupe.normalize_doi(doc.doi),
        "pmid": doc.pmid,
        "pmcid": doc.pmcid,
        "url": doc.url,
        "fulltext_url": doc.fulltext_url,
        "license": doc.license,
        "language": doc.language,
        "publication_types": list(doc.publication_types),
        "abstract": doc.abstract,
        "results": doc.results,
        "retraction_note": doc.retraction_note,
        "fetched_at": doc.fetched_at,
        "title_key": dedupe.title_key(doc.title, doc.year),
    }


def content_hash(doc: SourceDocument) -> str:
    return hashlib.sha256(doc.content_hash_basis.encode("utf-8")).hexdigest()


def collect_documents(
    cfg: EnrichmentConfig, today: dt.date
) -> tuple[list[SourceDocument], dict, list[str]]:
    """Опрашивает включённые базы. Ошибки одной базы не отменяют другую."""
    documents: list[SourceDocument] = []
    plan: dict = {}
    errors: list[str] = []
    if cfg.offline:
        return [], {"offline": True}, ["CC_ENRICH_OFFLINE=1: сетевые источники отключены"]

    if cfg.pubmed.enabled:
        try:
            docs, source_plan, source_errors = pubmed.fetch_documents(cfg.pubmed, today)
            documents.extend(docs)
            plan["pubmed"] = source_plan
            errors.extend(source_errors)
        except pubmed.SourceError as exc:
            errors.append(f"pubmed: {exc}")
            plan["pubmed"] = {"error": str(exc)}
    else:
        plan["pubmed"] = {"enabled": False}

    if cfg.who.enabled:
        try:
            docs, source_plan, source_errors = who.fetch_documents(cfg.who)
            documents.extend(docs)
            plan["who"] = source_plan
            errors.extend(source_errors)
        except who.SourceError as exc:
            errors.append(f"who: {exc}")
            plan["who"] = {"error": str(exc)}
    else:
        plan["who"] = {"enabled": False}

    return documents, plan, errors


def _decide(doc: SourceDocument, evaluation: criteria_mod.Evaluation, cfg: EnrichmentConfig) -> str:
    if evaluation.decision == "exclude":
        # Гайдлайны не выбрасываем молча: спорный гейт (лицензия/текст) → ревью.
        return "excluded" if doc.kind == "article" else "candidate"
    if doc.kind == "guideline" and cfg.auto_publish_guidelines:
        # Автопубликация только при подтверждённой лицензии: иначе индексация
        # all-rights-reserved текста нарушает условия ВОЗ. Неизвестное — не
        # «плохо», а «нужен человек» (ТЗ п. 5).
        license_verdict = evaluation.by_key("license")
        if license_verdict is not None and license_verdict.status == VERDICT_MET:
            return "approved"
    return "candidate"


def _apply_work(
    existing: KBWork | None,
    doc: SourceDocument,
    design_result,
    evaluation: criteria_mod.Evaluation,
    appraisal_result,
    status: str,
) -> tuple[KBWork, bool]:
    """Создаёт/обновляет запись реестра. Возвращает (work, content_changed)."""
    digest = content_hash(doc)
    verdicts = [asdict(v) for v in evaluation.verdicts]
    criteria_payload = {
        "version": CRITERIA_VERSION,
        "decision": evaluation.decision,
        "reasons": list(evaluation.reasons),
        "relevance_score": evaluation.relevance_score,
        "verdicts": verdicts,
    }
    appraisal_payload = asdict(appraisal_result)
    design_payload = asdict(design_result)

    if existing is None:
        work = KBWork(
            kind=doc.kind,
            source=doc.source,
            external_id=doc.external_id,
            guideline_id=doc.guideline_id,
            revision=doc.revision,
            content_hash=digest,
            status=status,
            title=doc.title,
            full_text=doc.text if doc.kind == "guideline" else "",
            meta=_meta(doc),
            design=design_payload,
            criteria=criteria_payload,
            appraisal=appraisal_payload,
            retraction=doc.retraction,
            revision_history=[
                {
                    "revision": doc.revision,
                    "content_hash": digest,
                    "fetched_at": doc.fetched_at,
                }
            ],
        )
        return work, True

    changed = existing.content_hash != digest
    existing.last_seen_at = dt.datetime.now(dt.timezone.utc)
    existing.revision = doc.revision
    existing.content_hash = digest
    existing.title = doc.title
    existing.full_text = doc.text if doc.kind == "guideline" else ""
    existing.meta = _meta(doc)
    existing.design = design_payload
    existing.criteria = criteria_payload
    existing.appraisal = appraisal_payload
    existing.retraction = doc.retraction
    if changed:
        history = list(existing.revision_history or [])
        history.append(
            {"revision": doc.revision, "content_hash": digest, "fetched_at": doc.fetched_at}
        )
        existing.revision_history = history[-20:]
    existing.status = status
    if status == "candidate" and changed:
        # Новая версия требует повторного решения врача.
        existing.approved_by = None
        existing.approved_at = None
    return existing, changed


async def _publish(work: KBWork, client, embedder, cfg: EnrichmentConfig) -> int:
    # Qdrant-клиент синхронный → в поток, чтобы не держать event loop.
    chunks = await anyio.to_thread.run_sync(
        lambda: index_mod.index_work(
            client, embedder, work,
            chunk_chars=cfg.chunk_chars, chunk_overlap=cfg.chunk_overlap,
        )
    )
    work.chunk_count = chunks
    work.published_at = dt.datetime.now(dt.timezone.utc)
    work.indexed_hash = work.content_hash
    return chunks


async def execute(
    db: AsyncSession,
    client,
    cfg: EnrichmentConfig,
    *,
    trigger: str = "cli",
    dry_run: bool = False,
    today: dt.date | None = None,
    documents: list[SourceDocument] | None = None,
    query_plan: dict | None = None,
    source_errors: list[str] | None = None,
) -> RunSummary:
    """Один прогон. documents/query_plan позволяют прогнать фикстуры оффлайн."""
    today = today or dt.date.today()
    embedder = build_embedder()
    run: KBRun | None = None
    run_id: str | None = None
    if not dry_run:
        run = KBRun(
            trigger=trigger,
            status="running",
            versions=_versions(cfg, embedder),
            query_plan=query_plan or {},
            stats={},
            errors=[],
        )
        db.add(run)
        await db.flush()
        # PK снимаем сразу: rollback в обработчике документа экспирит атрибуты,
        # и ленивая догрузка run.id в async-сессии дала бы MissingGreenlet.
        run_id = str(run.id)
        # Журнал прогона фиксируется сразу: откат отдельного документа
        # (rollback в обработчике) не должен лишать прогон записи в kb_runs.
        await db.commit()

    errors: list[str] = list(source_errors or [])
    if documents is None:
        documents, fetched_plan, fetch_errors = await anyio.to_thread.run_sync(
            lambda: collect_documents(cfg, today)
        )
        query_plan = query_plan or fetched_plan
        errors.extend(fetch_errors)
        if run is not None:
            # план должен попасть в журнал: он коммитится до сбора документов.
            run.query_plan = query_plan
            await db.commit()

    stats: dict[str, int] = {
        "fetched": len(documents),
        "duplicates": 0,
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "excluded": 0,
        "candidates": 0,
        "published": 0,
        "failed_documents": 0,
    }
    decisions: list[WorkDecision] = []

    seen: dict[str, str] = {}
    unique: list[SourceDocument] = []
    for doc in documents:
        duplicate = dedupe.duplicate_key(doc, seen)
        if duplicate:
            stats["duplicates"] += 1
            continue
        dedupe.remember(doc, seen)
        unique.append(doc)

    for doc in unique:
        # Транзакция на документ: запись реестра коммитится ДО публикации в
        # Qdrant, поэтому сбой коммита не оставляет в индексе точку без work.
        # Сбой ORM одного источника не мешает обработать остальные (partial).
        work: KBWork | None = None
        work_id = None
        published = False
        try:
            design_result = design_mod.classify(doc)
            evaluation = criteria_mod.evaluate(doc, design_result, today)
            appraisal_result = appraisal_mod.appraise(doc, design_result)
            status = _decide(doc, evaluation, cfg)

            existing = None if dry_run else await dedupe.find_existing(db, doc)
            changed = True
            if existing is not None:
                changed = existing.content_hash != content_hash(doc)
                if not changed:
                    stats["unchanged"] += 1
                    # Решение человека не сбрасывается повторным прогоном, пока
                    # содержимое версии не изменилось (иначе approve «сгорал» бы
                    # каждый день). Но новые нарушения критериев сильнее решения.
                    if (
                        existing.status in {"approved", "rejected"}
                        and evaluation.decision == "include"
                    ):
                        status = existing.status
                else:
                    stats["updated"] += 1
            else:
                stats["new"] += 1

            if evaluation.decision == "exclude":
                stats["excluded"] += 1
            if status == "candidate":
                stats["candidates"] += 1

            chunks = 0
            if dry_run:
                if status == "approved" and existing is not None:
                    chunks = existing.chunk_count
            else:
                work, _ = _apply_work(
                    existing, doc, design_result, evaluation, appraisal_result, status
                )
                if existing is None:
                    db.add(work)
                await db.flush()
                # id фиксируется до публикации: точки снимаются по нему, даже
                # если ORM-объект уже откатан.
                work_id = work.id
                await db.commit()

                if status == "approved":
                    # Сверка с фактическим индексом: коллекцию мог пересоздать
                    # kb-seed --reset (chunk_count > 0, а точек нет).
                    needs_publish = (
                        work.indexed_hash != work.content_hash or work.chunk_count == 0
                    )
                    if not needs_publish:
                        needs_publish = (
                            await anyio.to_thread.run_sync(index_mod.remote_count, client, work)
                        ) != work.chunk_count
                    if needs_publish:
                        chunks = await _publish(work, client, embedder, cfg)
                        published = True
                        stats["published"] += 1
                    else:
                        chunks = work.chunk_count
                elif status != "approved" and (work.indexed_hash or work.chunk_count):
                    # Снят с публикации (retraction/license/новая версия) — убрать чанки.
                    await anyio.to_thread.run_sync(index_mod.remove_work, client, work)
                    work.indexed_hash = None
                    work.chunk_count = 0
                # Фиксируем результат публикации (chunk_count/indexed_hash).
                await db.commit()

            decisions.append(
                WorkDecision(
                    source=doc.source,
                    external_id=doc.external_id,
                    kind=doc.kind,
                    title=doc.title,
                    design=design_result.design,
                    decision=evaluation.decision,
                    status=status,
                    relevance_score=evaluation.relevance_score,
                    reasons=evaluation.reasons,
                    appraisal_tool=appraisal_result.tool,
                    appraisal_summary=appraisal_result.summary,
                    chunks=chunks,
                    updated=changed,
                )
            )
        except Exception as exc:  # noqa: BLE001 — один документ не должен валить прогон
            await db.rollback()
            # Qdrant не участвует в транзакции: если публикация успела создать
            # точки, а коммит реестра — нет, снимаем их по work_id (best effort).
            if published and work_id is not None and client is not None:
                try:
                    await anyio.to_thread.run_sync(index_mod.remove_work_id, client, work_id)
                except Exception:  # noqa: BLE001 — уборка не должна маскировать исходную ошибку
                    pass
            stats["failed_documents"] += 1
            errors.append(f"{doc.source} {doc.external_id}: {exc}")

    if dry_run:
        status = "partial" if errors else "ok"
        return RunSummary(
            run_id=None,
            status=status,
            stats=stats,
            errors=errors,
            query_plan=query_plan or {},
            decisions=decisions,
        )

    assert run is not None
    run.stats = stats
    run.errors = errors
    run.finished_at = dt.datetime.now(dt.timezone.utc)
    if errors and stats["fetched"] == 0:
        run.status = "failed"
    elif errors:
        run.status = "partial"
    else:
        run.status = "ok"
    # Прогон фиксируется здесь: вызывающий код (CLI/сервис) не обязан помнить
    # про commit, иначе запись журнала терялась бы при его сбое.
    await db.commit()
    return RunSummary(
        run_id=run_id,
        status=run.status,
        stats=stats,
        errors=errors,
        query_plan=query_plan or {},
        decisions=decisions,
    )


async def latest_run(db: AsyncSession) -> KBRun | None:
    return await db.scalar(select(KBRun).order_by(KBRun.started_at.desc()).limit(1))
