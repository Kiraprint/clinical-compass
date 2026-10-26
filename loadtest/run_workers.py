"""Многопроцессный режим: `WEB_CONCURRENCY` против одного процесса uvicorn.

Закрывает остаточный риск отчёта «WEB_CONCURRENCY поддержан, но не измерялся».
Для каждого числа воркеров (по умолчанию 1, 2, 4) поднимается СВЕЖИЙ сервер с
пулом БД на процесс, и на нём снимаются `health` (предел HTTP-слоя) и
`diary_read` (JWT + Postgres + аудит) при C=16…96.

Пул на процесс подобран так, чтобы каждый воркер проходил
`app.db.validate_pool_budget`:

    N × (CC_DB_POOL_SIZE + CC_DB_MAX_OVERFLOW + 2 служебных) ≤ CC_DB_MAX_CONNECTIONS

то есть 1 → 20+20 (42 соединения), 2 → 20+20 (84), 4 → 8+8 (72) при потолке 100.
Если старт всё же отвергнут (например, CC_DB_MAX_CONNECTIONS занижен), ошибка
конфигурации попадает в results/workers.json как есть, а не превращается в
«сервер не поднялся».

Запуск (из корня worktree):

    python -m loadtest.run_workers --workers 1,2,4

Порты/БД/коллекция — переменными окружения (см. `loadtest/README.md`).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys
import time

from . import common

# Пул на процесс: (CC_DB_POOL_SIZE, CC_DB_MAX_OVERFLOW).
WORKER_POOL: dict[int, tuple[int, int]] = {1: (20, 20), 2: (20, 20), 4: (8, 8)}
DEFAULT_WORKERS = (1, 2, 4)
DEFAULT_LEVELS = (16, 32, 48, 64, 96)
SCENARIOS = ("health", "diary_read")
DRIFT_CONCURRENCY = 32


def loadavg() -> dict:
    one, five, fifteen = os.getloadavg()
    return {"1m": round(one, 2), "5m": round(five, 2), "15m": round(fifteen, 2)}


def pg_snapshot(dbname: str) -> dict:
    """Снимок pg_stat_activity: idle-in-transaction не должно быть вовсе.

    `idle` — это штатные соединения пула между запросами. `idle in transaction`
    означало бы сессию, удерживающую снимок и мешающую autovacuum.
    """
    import psycopg

    try:
        with psycopg.connect(
            common.pg_dsn("postgres"), autocommit=True, connect_timeout=5
        ) as conn:
            rows = conn.execute(
                "select coalesce(datname, '-'), coalesce(state, '-'), count(*) "
                "from pg_stat_activity where backend_type = 'client backend' "
                "group by 1, 2 order by 3 desc"
            ).fetchall()
            limit = conn.execute("show max_connections").fetchone()[0]
    except Exception as exc:  # noqa: BLE001 — снимок не должен ронять замер
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "max_connections": int(limit),
        "total": sum(count for _, _, count in rows),
        "loadtest_connections": sum(
            count for dat, _, count in rows if dat == dbname
        ),
        "idle_in_transaction": sum(
            count for _, state, count in rows if state.startswith("idle in transaction")
        ),
        "states": {
            f"{dat}/{state}": count for dat, state, count in sorted(rows)
        },
    }


def _probe(server, scenarios: dict, args) -> tuple[list[dict], dict]:
    """Все точки одного набора воркеров; порядок фиксирован (сценарий → C)."""
    points: list[dict] = []
    peak_pg: dict = {}
    for key in SCENARIOS:
        scenario = scenarios[key]
        for c in args.concurrency:
            result = asyncio.run(
                common.run_closed_loop(
                    scenario,
                    concurrency=c,
                    duration_s=args.duration,
                    warmup_s=args.warmup,
                    pid=server.proc.pid,
                    base_url=server.base_url,
                    tree=True,  # supervisor + N воркеров: CPU суммируется
                )
            )
            point = result.summary()
            point["key"] = key
            point["workers"] = server.workers
            point["loadavg"] = loadavg()
            point["pg"] = pg_snapshot(server.db)
            points.append(point)
            snapshot = point["pg"]
            if snapshot.get("loadtest_connections", 0) > peak_pg.get(
                "loadtest_connections", 0
            ):
                peak_pg = dict(snapshot, concurrency=c, scenario=key)
            print(
                f"W={server.workers} {key:10s} C={c:3d}  rps={point['rps']:8.1f}  "
                f"p50={point['latency_ms']['p50']:7.1f}ms  "
                f"p95={point['latency_ms']['p95']:7.1f}ms  "
                f"p99={point['latency_ms']['p99']:7.1f}ms  "
                f"err={point['error_rate']:.2%}  "
                f"cpu={point['resources'].get('cpu_percent_one_core', 0):6.0f}%  "
                f"db={snapshot.get('loadtest_connections', '?')} "
                f"idle_tx={snapshot.get('idle_in_transaction', '?')}",
                flush=True,
            )
    return points, peak_pg


def _summarize(blocks: list[dict]) -> dict:
    """Лучший устойчивый RPS на набор воркеров и масштаб относительно одного."""
    summary: dict[str, dict] = {}
    for block in blocks:
        if "points" not in block:
            continue
        workers = block["workers"]
        per_scenario: dict[str, dict] = {}
        for key in SCENARIOS:
            points = [p for p in block["points"] if p["key"] == key]
            if not points:
                continue
            stable = [p for p in points if p["error_rate"] < 0.01]
            best = max(stable, key=lambda p: p["rps"]) if stable else None
            first = min(points, key=lambda p: p["concurrency"])
            per_scenario[key] = {
                "best_rps": round(best["rps"], 1) if best else 0.0,
                "best_concurrency": best["concurrency"] if best else None,
                "latency_at_best": best["latency_ms"] if best else None,
                "errors_at_best": best["errors"] if best else None,
                "first_rps": round(first["rps"], 1),
                "first_concurrency": first["concurrency"],
                "error_total": sum(p["errors"] for p in points),
                "error_rate_max": max(p["error_rate"] for p in points),
                "peak_cpu_percent_one_core": max(
                    p["resources"].get("cpu_percent_one_core", 0) for p in points
                ),
                "cpu_at_best": best["resources"].get("cpu_percent_one_core")
                if best else None,
                "client_cpu_at_best": best["resources"].get(
                    "client_cpu_percent_one_core"
                ) if best else None,
                "client_cpu_max": max(
                    p["resources"].get("client_cpu_percent_one_core", 0) or 0
                    for p in points
                ),
                "peak_rss_mb": max(
                    p["resources"].get("rss_mb", 0) for p in points
                ),
                "processes": max(
                    p["resources"].get("processes", 1) for p in points
                ),
                "saturation": _saturation(points),
            }
        summary[str(workers)] = {
            "workers": workers,
            "budget": block["budget"],
            "startup_s": block.get("startup_s"),
            "points": per_scenario,
        }

    base = summary.get("1", {}).get("points", {})
    for block in summary.values():
        for key, row in block["points"].items():
            first = base.get(key, {}).get("best_rps") or 0.0
            row["scaling_vs_1"] = (
                round(row["best_rps"] / first, 2) if first else None
            )
    return summary


def _saturation(points: list[dict]) -> dict:
    """Где RPS перестал расти и/или появились ошибки."""
    ordered = sorted(points, key=lambda p: p["concurrency"])
    best = max(ordered, key=lambda p: p["rps"])
    tail = [p for p in ordered if p["concurrency"] > best["concurrency"]]
    errors = [p for p in ordered if p["error_rate"] >= 0.01]
    return {
        "peak_concurrency": best["concurrency"],
        "peak_rps": round(best["rps"], 1),
        "rps_after_peak": [
            {"concurrency": p["concurrency"], "rps": round(p["rps"], 1)}
            for p in tail
        ],
        "first_error_concurrency": errors[0]["concurrency"] if errors else None,
        "first_error_rate": errors[0]["error_rate"] if errors else None,
        "first_error_types": errors[0]["error_types"] if errors else None,
    }


def _host() -> dict:
    cpu = ""
    try:
        for line in open("/proc/cpuinfo", encoding="utf-8"):
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "cpu_model": cpu,
        "python": sys.version.split()[0],
        "loadavg_before": loadavg(),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="WEB_CONCURRENCY: 1/2/4 процесса uvicorn под нагрузкой"
    )
    parser.add_argument(
        "--workers", default=",".join(str(w) for w in DEFAULT_WORKERS),
        help="наборы воркеров через запятую (по умолчанию 1,2,4)",
    )
    parser.add_argument(
        "--concurrency", default=",".join(str(c) for c in DEFAULT_LEVELS),
        help="уровни конкуренции через запятую (по умолчанию 16,32,48,64,96)",
    )
    parser.add_argument("--duration", type=float, default=8.0, help="с на точку")
    parser.add_argument("--warmup", type=float, default=0.75, help="с прогрева")
    parser.add_argument(
        "--port", type=int, default=common.API_PORT, help="порт стенда (по умолчанию 8011)"
    )
    parser.add_argument("--embedder", default="hash", choices=["hash", "bge-m3"])
    parser.add_argument("--db", default=None)
    parser.add_argument("--collection", default=None)
    parser.add_argument("--skip-provision", action="store_true")
    parser.add_argument("--out", default="workers.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.workers_list = [int(w) for w in args.workers.split(",") if w.strip()]
    args.concurrency = [int(c) for c in args.concurrency.split(",") if c.strip()]

    common.apply_env(embedder=args.embedder, collection=args.collection, db=args.db)

    from . import provision, run_http, serve

    payload: dict = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": _host(),
        "git": common.git_provenance(),
        "database": common.resolve_db(args.db),
        "collection": common.resolve_collection(args.collection),
        "port": args.port,
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "levels": args.concurrency,
        "scenarios": list(SCENARIOS),
    }

    if not args.skip_provision:
        payload["provision"] = provision.provision(
            embedder=args.embedder, collection=args.collection, db=args.db
        )
    credentials = provision.load_credentials()
    scenarios = run_http.build_scenarios(credentials)

    blocks: list[dict] = []
    drift_first: dict | None = None
    failures = 0
    for workers in args.workers_list:
        pool_size, overflow = WORKER_POOL.get(
            workers, (common.DEFAULT_POOL_SIZE, common.DEFAULT_MAX_OVERFLOW)
        )
        server = serve.ApiServer(
            embedder=args.embedder,
            collection=args.collection,
            db=args.db,
            port=args.port,
            pool_size=pool_size,
            max_overflow=overflow,
            workers=workers,
            log_name=f"api_w{workers}.log",
        )
        block: dict = {
            "workers": workers,
            "budget": server.budget,
            "level_pool": WORKER_POOL.get(workers),
        }
        print(f"\n=== workers={workers} pool={pool_size}+{overflow} "
              f"budget: {server.budget['formula']}", flush=True)
        try:
            server.start()
        except RuntimeError as exc:
            # Ошибка конфигурации (в т.ч. отказ validate_pool_budget) — в отчёт
            # как есть: молча пропустить набор значило бы скрыть причину.
            block["error"] = str(exc)
            block["log_tail"] = server.log_tail()
            failures += 1
            print(f"ОТКАЗ workers={workers}: {exc}", file=sys.stderr, flush=True)
            blocks.append(block)
            server.stop()
            continue
        block["startup_s"] = round(server.startup_s, 2)
        block["worker_pids"] = server.worker_pids()
        try:
            points, peak_pg = _probe(server, scenarios, args)
            block["points"] = points
            block["peak_pg"] = peak_pg
            block["log_tail"] = server.log_tail(15)
            if workers == 1 and drift_first is None:
                # Якорь дрейфа — health при C=DRIFT_CONCURRENCY. Если такого
                # уровня/сценария в прогоне нет (--concurrency), контроля дрейфа
                # просто не будет: падать из-за этого нельзя.
                drift_first = next(
                    (
                        p for p in points
                        if p["key"] == "health"
                        and p["concurrency"] == DRIFT_CONCURRENCY
                    ),
                    None,
                )
            # Снимок с ещё открытыми соединениями пула: idle-in-transaction тут
            # означал бы застрявшую транзакцию, а не штатный idle между запросами.
            block["pg_before_stop"] = pg_snapshot(server.db)
        finally:
            server.stop()
        block["pg_after_stop"] = pg_snapshot(server.db)
        for label in ("pg_before_stop", "pg_after_stop"):
            snapshot = block[label]
            print(
                f"{label} W={workers}: idle_in_transaction="
                f"{snapshot.get('idle_in_transaction', '?')}, "
                f"соединений стенда={snapshot.get('loadtest_connections', '?')}, "
                f"всего={snapshot.get('total', '?')}/"
                f"{snapshot.get('max_connections', '?')}",
                flush=True,
            )
        blocks.append(block)

    # Контроль дрейфа машины: те же 1 воркер + health C=32 в конце прогона.
    # Разница с первой точкой — вклад чужой нагрузки/троттлинга, а не воркеров.
    if drift_first is not None and 1 in args.workers_list:
        server = serve.ApiServer(
            embedder=args.embedder, collection=args.collection, db=args.db,
            port=args.port, pool_size=WORKER_POOL[1][0],
            max_overflow=WORKER_POOL[1][1], workers=1, log_name="api_drift.log",
        )
        try:
            server.start()
            point = asyncio.run(
                common.run_closed_loop(
                    scenarios["health"], concurrency=DRIFT_CONCURRENCY,
                    duration_s=args.duration, warmup_s=args.warmup,
                    pid=server.proc.pid, base_url=server.base_url, tree=True,
                )
            )
            print(f"W=1 drift control C={DRIFT_CONCURRENCY} rps={point.summary()['rps']}",
                  flush=True)
            payload["drift"] = {
                "workers": 1,
                "concurrency": DRIFT_CONCURRENCY,
                "first_rps": drift_first["rps"],
                "last_rps": point.summary()["rps"],
                "ratio": round(point.summary()["rps"] / drift_first["rps"], 3)
                if drift_first["rps"] else None,
            }
        finally:
            server.stop()

    payload["worker_configs"] = blocks
    payload["summary"] = _summarize(blocks)
    payload["failures"] = failures
    payload["host"]["loadavg_after"] = loadavg()
    payload["pg_final"] = pg_snapshot(payload["database"])
    path = common.write_json(args.out, payload)
    print(f"\nРезультаты: {path}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
