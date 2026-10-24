"""Оркестратор: provision → API → ML-метрики → HTTP-нагрузка → ядро → отчёт."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

from . import common


def _idle_rss_mb(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Нагрузочные замеры Clinical Compass")
    parser.add_argument("--quick", action="store_true", help="мало уровней/короткие прогоны")
    parser.add_argument("--embedder", default="hash", choices=["hash", "bge-m3"])
    parser.add_argument(
        "--collection",
        default=None,
        help=f"коллекция Qdrant (allowlist: {common.COLLECTION_PREFIX}*); по умолчанию "
        f"{common.DEFAULT_COLLECTION}",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"БД (allowlist: {common.DB_PREFIX}*); по умолчанию {common.DEFAULT_DB}",
    )
    parser.add_argument("--skip-provision", action="store_true")
    parser.add_argument("--skip-http", action="store_true")
    parser.add_argument("--skip-core", action="store_true")
    parser.add_argument("--skip-ml", action="store_true")
    args = parser.parse_args(argv)

    common.apply_env(
        embedder=args.embedder, collection=args.collection, db=args.db
    )

    from . import provision, run_core, run_http, run_search, serve

    meta = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": {
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "python": sys.version.split()[0],
        },
        "git": common.git_provenance(),
        "embedder": args.embedder,
        "collection": common.resolve_collection(args.collection),
        "database": common.resolve_db(args.db),
        "llm_url": os.environ.get("CC_ASSISTANT_LLM_URL", ""),
        "quick": args.quick,
    }

    if not args.skip_provision:
        meta["provision"] = provision.provision(
            embedder=args.embedder, collection=args.collection, db=args.db
        )
    credentials = provision.load_credentials()

    server = serve.ApiServer(embedder=args.embedder, collection=args.collection)
    server.start()
    meta["server"] = {
        "startup_s": round(server.startup_s, 2),
        "idle_rss_mb": _idle_rss_mb(server.proc.pid) if server.proc else 0.0,
        "url": server.base_url,
    }
    print(f"API: {server.base_url}, старт {meta['server']['startup_s']}s, "
          f"RSS {meta['server']['idle_rss_mb']} MB")

    try:
        if not args.skip_ml:
            meta["ml"] = run_search.run(credentials)["summary"]
        if not args.skip_http:
            run_http.run(credentials, pid=server.proc.pid, quick=args.quick)
        if not args.skip_core:
            run_core.run()
    finally:
        server.stop()

    if not args.skip_http:
        run_http.run_cliff(
            credentials, quick=args.quick,
            embedder=args.embedder, collection=args.collection,
        )
    common.write_json("meta.json", meta)
    from . import report

    report.build()
    print("\nГотово: loadtest/REPORT.md, loadtest/results/*.json")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except common.SafetyRefusal as exc:
        print(f"ОТКАЗ: {exc}", file=sys.stderr)
        raise SystemExit(common.SafetyRefusal.exit_code) from exc
