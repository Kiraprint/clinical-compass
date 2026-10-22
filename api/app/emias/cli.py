"""CLI интеграции с ЕМИАС (FR-25): `python -m app.emias.cli <команда>`.

Команды:
  sync-patients             обход контура ЕМИАС, upsert связей и истории
  sync-patient <uuid>       обновить одного пациента (нужна emias_links)
  link <uuid> <СНИЛС>       связать пациента с ЕМИАС по СНИЛС
  health                    флаг интеграции и последний прогон
  runs [--limit N]          история прогонов emias_sync_runs

Формат вывода — JSON (как у app.enrichment.cli), ПДн/токены не печатаются.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from app import db as db_module
from app.config import settings

def _session():
    if db_module.engine is None:
        db_module.setup(settings.database_url)
    return db_module.SessionLocal()

def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

def _disabled() -> int:
    _print({"error": "emias_disabled", "hint": "CC_EMIAS_ENABLED=true"})
    return 2

def _cmd_health(_args) -> int:
    from app.emias.config import current_config
    from app.models import EmiasSyncRun

    cfg = current_config()
    with _session() as session:
        last = session.scalar(select(EmiasSyncRun).order_by(EmiasSyncRun.started_at.desc()).limit(1))
        payload = {
            "enabled": cfg.enabled,
            "base_url_scheme": cfg.base_url.split(":", 1)[0],
            "last_run": None if last is None else {
                "id": str(last.id), "mode": last.mode, "status": last.status,
                "stats": last.stats, "started_at": last.started_at,
            },
        }
    _print(payload)
    return 0

def _cmd_sync_patients(_args) -> int:
    if not settings.emias_enabled:
        return _disabled()
    from app.emias.sync import execute_sync

    with _session() as session:
        run = execute_sync(session, mode="patients", trigger="cli")
        _print({"run_id": str(run.id), "status": run.status, "stats": run.stats})
    return 0 if run.status in ("ok", "partial") else 1

def _cmd_sync_patient(args) -> int:
    if not settings.emias_enabled:
        return _disabled()
    from app.emias.sync import execute_sync

    with _session() as session:
        run = execute_sync(session, mode="one", patient_id=args.patient_id, trigger="cli")
        _print({"run_id": str(run.id), "status": run.status, "stats": run.stats})
    return 0 if run.status in ("ok", "partial") else 1

def _cmd_link(args) -> int:
    if not settings.emias_enabled:
        return _disabled()
    from app.emias.sync import link_patient
    from app.errors import Problem

    try:
        with _session() as session:
            result = link_patient(session, args.patient_id, args.snils)
    except Problem as exc:
        _print({"error": exc.code, "detail": exc.detail})
        return 1
    _print(result)
    return 0

def _cmd_runs(args) -> int:
    from app.models import EmiasSyncRun

    with _session() as session:
        rows = session.scalars(
            select(EmiasSyncRun).order_by(EmiasSyncRun.started_at.desc()).limit(args.limit)
        ).all()
        _print({"items": [
            {"id": str(r.id), "trigger": r.trigger, "mode": r.mode, "status": r.status,
             "stats": r.stats, "started_at": r.started_at, "finished_at": r.finished_at}
            for r in rows
        ]})
    return 0

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.emias.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync-patients")
    one = sub.add_parser("sync-patient")
    one.add_argument("patient_id")
    link = sub.add_parser("link")
    link.add_argument("patient_id")
    link.add_argument("snils")
    sub.add_parser("health")
    runs = sub.add_parser("runs")
    runs.add_argument("--limit", type=int, default=10)

    args = parser.parse_args(argv)
    handlers = {
        "sync-patients": _cmd_sync_patients,
        "sync-patient": _cmd_sync_patient,
        "link": _cmd_link,
        "health": _cmd_health,
        "runs": _cmd_runs,
    }
    return handlers[args.command](args)

if __name__ == "__main__":
    sys.exit(main())
