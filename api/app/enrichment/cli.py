"""CLI сервиса kb-enrichment: `python -m app.enrichment <команда>`.

Команды:
  run [--dry-run] [--source pubmed|who]   один прогон
  serve                                   цикл по CC_ENRICH_INTERVAL_HOURS
  review list|show|approve|reject         ревью источников (врач/админ)
  runs [--limit N]                        история прогонов (воспроизводимость)
  reindex [--work-id UUID] [--dry-run]    перепубликация approved-работ в Qdrant
"""

from __future__ import annotations

import argparse
import anyio
import asyncio
import datetime as dt
import json
import sys
import time

from sqlalchemy import select, text

from app import db as db_module
from app.config import settings
from app.enrichment import CRITERIA_VERSION, pipeline, review
from app.enrichment.config import EnrichmentConfig
from app.models import KBRun, KBWork


def _setup() -> None:
    if db_module.engine is None:
        db_module.setup(settings.database_url, profile="background")


def _session():
    _setup()
    return db_module.AsyncSessionLocal()


def _engine():
    _setup()
    return db_module.engine


def _client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=settings.qdrant_url)

def _print(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

async def _cmd_run(args) -> int:
    cfg = EnrichmentConfig.from_env()
    if args.source == "pubmed":
        cfg = _with_enabled(cfg, pubmed=True, who=False)
    elif args.source == "who":
        cfg = _with_enabled(cfg, pubmed=False, who=True)
    async with _session() as session:
        summary = await pipeline.execute(
            session,
            None if args.dry_run else _client(),
            cfg,
            trigger="cli",
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            await session.commit()
    _print(summary.as_dict(), args.json)
    return 0 if summary.status in {"ok", "partial"} else 1

def _with_enabled(cfg: EnrichmentConfig, *, pubmed: bool, who: bool) -> EnrichmentConfig:
    from dataclasses import replace

    return replace(
        cfg,
        pubmed=replace(cfg.pubmed, enabled=pubmed),
        who=replace(cfg.who, enabled=who),
    )

async def _cmd_serve(args) -> int:
    cfg = EnrichmentConfig.from_env()
    interval = max(cfg.interval_hours, 0.05) * 3600
    if not cfg.run_on_start:
        await asyncio.sleep(interval)
    while True:
        # Advisory-лок живёт на соединении, а не на транзакции: async-сессия
        # после commit возвращает соединение в пул, поэтому держим отдельное.
        async with _engine().connect() as lock_conn:
            locked = bool(
                (await lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtext('kb-enrichment'))")
                )).scalar()
            )
            if not locked:
                print("kb-enrichment: другой прогон уже идёт, пропуск", flush=True)
            else:
                try:
                    async with _session() as session:
                        summary = await pipeline.execute(session, _client(), cfg, trigger="schedule")
                        await session.commit()
                        print(
                            f"kb-enrichment: run {summary.run_id} {summary.status} "
                            f"{summary.stats} errors={len(summary.errors)}",
                            flush=True,
                        )
                except Exception as exc:  # noqa: BLE001 — сервис не должен падать на одном прогоне
                    # сессия закрывается в context manager выше → неявный rollback
                    print(f"kb-enrichment: прогон упал: {exc}", flush=True)
                finally:
                    # Shield: отмена посреди прогона не должна оставить
                    # session-level лок на соединении из пула.
                    with anyio.CancelScope(shield=True):
                        try:
                            await lock_conn.execute(
                                text("SELECT pg_advisory_unlock(hashtext('kb-enrichment'))")
                            )
                            await lock_conn.commit()
                        except Exception:  # noqa: BLE001 — разрыв соединения снимет лок сам
                            print("kb-enrichment: не удалось снять advisory-лок", flush=True)
        await asyncio.sleep(interval)

async def _cmd_review(args) -> int:
    cfg = EnrichmentConfig.from_env()
    async with _session() as session:
        if args.action == "list":
            works = await review.list_works(
                session, status=args.status, kind=args.kind, limit=args.limit
            )
            payload = [review.serialize(w) for w in works]
        elif args.action == "show":
            work = await review.get_work(session, _uuid(args.id))
            if work is None:
                print("не найдено", file=sys.stderr)
                return 2
            payload = review.serialize(work)
        elif args.action == "approve":
            work_id = _uuid(args.id)
            # review.approve сам коммитит статус до индексации и откатывает его
            # при сбое публикации — компенсация в CLI больше не нужна.
            work = await review.approve(session, _client(), work_id, None, cfg)
            payload = review.serialize(work)
        elif args.action == "reject":
            work = await review.reject(
                session, _uuid(args.id), None, args.reason, client=_client()
            )
            await session.commit()
            payload = review.serialize(work)
        else:  # pragma: no cover - argparse ограничивает выбор
            raise SystemExit(f"неизвестное действие {args.action}")
    _print(payload, args.json)
    return 0

async def _cmd_runs(args) -> int:
    async with _session() as session:
        rows = (await session.scalars(
            select(KBRun).order_by(KBRun.started_at.desc()).limit(args.limit)
        )).all()
        payload = [
            {
                "id": str(run.id),
                "trigger": run.trigger,
                "status": run.status,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "stats": run.stats,
                "errors": run.errors,
                "versions": run.versions,
                "query_plan": run.query_plan,
            }
            for run in rows
        ]
    _print(payload, args.json)
    return 0

async def _cmd_reindex(args) -> int:
    """Перепубликация approved-работ в Qdrant — восстановление производного индекса.

    Нужна после сброса коллекции снаружи (`kb_seed --reset`, смена эмбеддера):
    реестр kb_works остаётся источником истины, а прогон `run` для той же сверки
    потребовал бы похода в PubMed/IRIS. Порядок как в pipeline.execute: реестр
    читается и отвязывается от сессии до сетевого вызова, chunk_count
    записывается короткой транзакцией после.
    """
    from app.enrichment import index as index_mod
    from app.services.embeddings import get_embedder

    cfg = EnrichmentConfig.from_env()
    embedder = get_embedder()
    client = None if args.dry_run else _client()

    if args.work_id is None:
        query = select(KBWork).where(KBWork.status == "approved").order_by(KBWork.guideline_id)
    else:
        query = select(KBWork).where(KBWork.id == args.work_id)
    async with _session() as session:
        rows = list((await session.scalars(query)).all())
        # Снимок отвязывается от сессии: сетевой вызов не должен идти внутри
        # открытой транзакции.
        for work in rows:
            session.expunge(work)

    reindexed = 0
    skipped = 0
    errors: list[str] = []
    stale: list[str] = []
    for work in rows:
        if work.status != "approved":
            # Возможно только с --work-id: без него статус отфильтрован в SQL.
            skipped += 1
            continue
        # Вердикты критериев хранятся на момент ингеста: работу, одобренную до
        # появления нового жёсткого критерия, нельзя переопубликовать вслепую.
        if (work.criteria or {}).get("version") != CRITERIA_VERSION:
            stale.append(f"{work.guideline_id}: критерии {((work.criteria or {}).get('version') or '—')} != {CRITERIA_VERSION}")
            skipped += 1
            continue
        if args.dry_run:
            reindexed += 1
            print(f"dry-run: {work.guideline_id} (чанков в реестре: {work.chunk_count})")
            continue
        try:
            chunks = await anyio.to_thread.run_sync(
                lambda work=work: index_mod.index_work(
                    client,
                    embedder,
                    work,
                    chunk_chars=cfg.chunk_chars,
                    chunk_overlap=cfg.chunk_overlap,
                )
            )
        except Exception as exc:  # noqa: BLE001 — одна работа не валит остальные
            errors.append(f"{work.guideline_id}: {exc}")
            continue
        await _record_reindex(work.id, work.content_hash, chunks)
        reindexed += 1

    if args.dry_run:
        print(f"dry-run: будет переиндексировано {reindexed}, пропущено {skipped}")
        for note in stale:
            print(f"критерии устарели: {note}", file=sys.stderr)
        return 0
    print(f"переиндексировано {reindexed}, пропущено {skipped}, ошибок {len(errors)}")
    for note in stale:
        print(f"критерии устарели: {note} — нужен повторный run", file=sys.stderr)
    for error in errors:
        print(f"ошибка: {error}", file=sys.stderr)
    return 1 if errors else 0

async def _record_reindex(work_id, content_hash: str, chunks: int) -> None:
    """Короткая транзакция после индексации: сеть внутри неё не выполняется.

    Сверка content_hash не даёт записать chunk_count для работы, которую
    параллельный прогон успел заменить новой версией.
    """
    async with _session() as session:
        work = await session.get(KBWork, work_id)
        if work is None or work.status != "approved" or work.content_hash != content_hash:
            return
        work.chunk_count = chunks
        work.indexed_hash = content_hash
        work.published_at = dt.datetime.now(dt.timezone.utc)
        await session.commit()

def _uuid(value: str):
    import uuid

    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise SystemExit(f"некорректный UUID: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.enrichment", description="Обогащение КБ (PubMed + ВОЗ)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="один прогон ингеста")
    run.add_argument("--dry-run", action="store_true", help="без записи в БД и Qdrant")
    run.add_argument("--source", choices=["all", "pubmed", "who"], default="all")
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=_cmd_run)

    serve = sub.add_parser("serve", help="цикл прогонов (cron-замена)")
    serve.set_defaults(func=_cmd_serve)

    review_parser = sub.add_parser("review", help="ревью источников")
    review_parser.add_argument("action", choices=["list", "show", "approve", "reject"])
    review_parser.add_argument("id", nargs="?")
    review_parser.add_argument("--status", default="candidate")
    review_parser.add_argument("--kind", choices=["article", "guideline"], default=None)
    review_parser.add_argument("--limit", type=int, default=50)
    review_parser.add_argument("--reason", default="")
    review_parser.add_argument("--json", action="store_true")
    review_parser.set_defaults(func=_cmd_review)

    runs = sub.add_parser("runs", help="история прогонов")
    runs.add_argument("--limit", type=int, default=10)
    runs.add_argument("--json", action="store_true")
    runs.set_defaults(func=_cmd_runs)

    reindex = sub.add_parser("reindex", help="перепубликовать approved-работы в Qdrant")
    reindex.add_argument("--work-id", type=_uuid, default=None, help="только одна работа")
    reindex.add_argument("--dry-run", action="store_true", help="только показать план")
    reindex.set_defaults(func=_cmd_reindex)
    return parser


async def _run_and_dispose(func, args) -> int:
    """Выполнить команду и закрыть пул в том же event loop (иначе dispose из
    другого цикла получает соединения, привязанные к чужому loop)."""
    try:
        return await func(args)
    finally:
        # Shield: при отмене (SIGTERM) закрытие пула всё равно должно выполниться.
        with anyio.CancelScope(shield=True):
            if db_module.engine is not None:
                await db_module.engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run_and_dispose(args.func, args))
    except review.ReviewError as exc:
        print(f"ошибка ревью: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
