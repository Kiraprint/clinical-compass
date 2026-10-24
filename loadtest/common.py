"""Общая обвязка нагрузочных замеров Clinical Compass.

Замеры изолированы: отдельная БД `compass_loadtest`, отдельная коллекция Qdrant
`kb_loadtest`, отдельный процесс uvicorn на порту 8011. Прод/демо-стек не
затрагивается.

Жёсткий предохранитель: имена целей обязаны попадать в allowlist
(`compass_load*` / `kb_load*`), а `DROP DATABASE` и пересоздание коллекции
дополнительно требуют `CC_LOADTEST_ALLOW=1`. Проверка выполняется до любого
соединения (`assert_safe` / `assert_destructive`), отказ называет значение.

Запуск: `python -m loadtest.run_all` (из корня worktree); проверка целей без
соединений: `python -m loadtest.provision --dry-run`.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS_DIR = HERE / "results"
API_DIR = ROOT / "api"
CORE_DIR = ROOT / "core"
PYTHON = os.environ.get("CC_LOADTEST_PYTHON", sys.executable)

API_HOST = os.environ.get("CC_LOADTEST_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("CC_LOADTEST_PORT", "8011"))
BASE_URL = f"http://{API_HOST}:{API_PORT}"

# В worktree пакеты app/compass должны резолвиться отсюда, а не из editable
# install основного чек-аута (иначе схема и корпус — из другой ветки).
for _path in (str(CORE_DIR), str(API_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# ---------------------------------------------------------------------------
# Целевые ресурсы и защитный allowlist.
#
# Стенд умеет `DROP DATABASE ... WITH FORCE` и пересоздавать коллекцию Qdrant.
# Чтобы опечатка в CC_LOADTEST_DB/--db не снесла прод, имя БД обязано начинаться
# с `compass_load`, имя коллекции — с `kb_load`; разрушающие операции требуют
# CC_LOADTEST_ALLOW=1. Проверка идёт ДО любого соединения.
# ---------------------------------------------------------------------------
DB_PREFIX = "compass_load"
COLLECTION_PREFIX = "kb_load"
DEFAULT_DB = "compass_loadtest"
DEFAULT_COLLECTION = "kb_loadtest"
ALLOW_ENV = "CC_LOADTEST_ALLOW"

PG_HOST = "127.0.0.1"
PG_PORT = 5432
PG_USER = "compass"
PG_PASSWORD = "compass"

QDRANT_URL = os.environ.get("CC_QDRANT_URL", "http://127.0.0.1:6333")


class SafetyRefusal(RuntimeError):
    """Имя ресурса вне allowlist или нет CC_LOADTEST_ALLOW=1."""

    exit_code = 2


def resolve_db(name: str | None = None) -> str:
    """Явный аргумент → CC_LOADTEST_DB → compass_loadtest (резолв в момент вызова)."""
    return name or os.environ.get("CC_LOADTEST_DB") or DEFAULT_DB


def resolve_collection(name: str | None = None) -> str:
    """Явный аргумент → CC_LOADTEST_COLLECTION → kb_loadtest."""
    return name or os.environ.get("CC_LOADTEST_COLLECTION") or DEFAULT_COLLECTION


def db_url(name: str | None = None) -> str:
    return f"postgresql+psycopg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{resolve_db(name)}"


def redacted_db_url(name: str | None = None) -> str:
    return f"postgresql+psycopg://{PG_USER}:***@{PG_HOST}:{PG_PORT}/{resolve_db(name)}"


def _check_name(kind: str, value: str, prefix: str, env_var: str, flag: str) -> str:
    # Только [a-z0-9_] и длина ≤ 48: имя подставляется в DDL (DROP DATABASE),
    # поэтому кавычки/точки с запятой должны отсекаться до любого соединения.
    if not value.startswith(prefix):
        raise SafetyRefusal(
            f"{kind} {value!r} не разрешено: имя должно начинаться с {prefix!r} "
            f"(по умолчанию {prefix}test). Исправьте {env_var} или {flag}."
        )
    if not re.fullmatch(r"[a-z0-9_]{1,48}", value):
        raise SafetyRefusal(
            f"{kind} {value!r} не разрешено: только строчные латинские буквы, "
            f"цифры и подчёркивание (≤48 символов)."
        )
    return value


def assert_safe(db: str | None = None, collection: str | None = None) -> tuple[str, str]:
    """Проверить оба имени до соединения; вернуть (db, collection)."""
    db_name = _check_name("БД", resolve_db(db), DB_PREFIX, "CC_LOADTEST_DB", "--db")
    coll = _check_name(
        "Коллекция Qdrant",
        resolve_collection(collection),
        COLLECTION_PREFIX,
        "CC_LOADTEST_COLLECTION",
        "--collection",
    )
    return db_name, coll


def assert_destructive(
    db: str | None = None, collection: str | None = None, action: str = "provision"
) -> tuple[str, str]:
    """Allowlist + CC_LOADTEST_ALLOW=1: для DROP DATABASE и пересоздания коллекции."""
    db_name, coll = assert_safe(db, collection)
    allow = os.environ.get(ALLOW_ENV)
    if allow != "1":
        raise SafetyRefusal(
            f"{action}: разрушающие операции запрещены без {ALLOW_ENV}=1 "
            f"(сейчас {allow or 'не задан'}). Будут затронуты БД {db_name!r} и "
            f"коллекция {coll!r}."
        )
    return db_name, coll


def describe_target(db: str | None = None, collection: str | None = None) -> str:
    db_name, coll = assert_safe(db, collection)
    return (
        f"БД: {redacted_db_url(db_name)}\n"
        f"Коллекция Qdrant: {coll} @ {QDRANT_URL}\n"
        f"Разрушающие операции: {ALLOW_ENV}={os.environ.get(ALLOW_ENV) or 'не задан'}"
    )


def git_provenance() -> dict:
    """Коммит и грязность дерева — для провенанса отчёта."""

    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        )
        return result.stdout.strip()

    changed = [line for line in run("status", "--porcelain").splitlines() if line]
    return {
        "commit": run("rev-parse", "--short", "HEAD") or "—",
        "dirty": bool(changed),
        "changed_files": len(changed),
    }


def apply_env(
    embedder: str = "hash", collection: str | None = None, db: str | None = None
) -> dict[str, str]:
    """Зафиксировать env стенда в os.environ и вернуть его. Вызывать ДО импорта app.*"""
    env = setup_env(embedder=embedder, collection=collection, db=db)
    os.environ.update(env)
    return env


def bench_data_dir() -> Path:
    """bench/data не коммитится (сторонний контент): ищем в worktree, затем в
    основном чек-ауте репозитория; переопределение — CC_LOADTEST_BENCH_DATA."""
    override = os.environ.get("CC_LOADTEST_BENCH_DATA")
    candidates = [Path(override)] if override else []
    candidates += [ROOT / "bench" / "data", ROOT.parents[1] / "bench" / "data"]
    for candidate in candidates:
        if (candidate / "queries.jsonl").exists() and (candidate / "corpus.jsonl").exists():
            return candidate
    raise SystemExit(
        "Нет bench/data/{queries,corpus}.jsonl. Соберите набор: "
        "cd bench && .venv/bin/python fetch_sources.py && "
        ".venv/bin/python build_dataset.py && .venv/bin/python expand_queries.py"
    )

# Демо-секреты разрешены только этим флагом; фиксированный JWT-секрет нужен,
# чтобы provision и API-процесс подписывали/проверяли одни и те же токены.
JWT_SECRET = "loadtest-jwt-secret-0123456789abcdef"
PASSWORD = "loadtest-pass-123"

PATIENTS = 256
CLINICIANS = 8

# Ресурсные замеры: uvicorn — один процесс.
@dataclass
class ResourceSample:
    rss_kb: int = 0
    cpu_seconds: float = 0.0
    threads: int = 0


def _read_status(pid: int) -> ResourceSample:
    sample = ResourceSample()
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return sample
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            sample.rss_kb = int(line.split()[1])
        elif line.startswith("Threads:"):
            sample.threads = int(line.split()[1])
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        sample.cpu_seconds = (int(fields[13]) + int(fields[14])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        pass
    return sample


class ResourceSampler:
    """Пиковый RSS/CPU процесса API за время замера."""

    def __init__(self, pid: int):
        self.pid = pid
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.peak = ResourceSample()
        self.start_sample = ResourceSample()
        self.end_sample = ResourceSample()

    def _loop(self) -> None:
        while not self._stop.wait(0.25):
            s = _read_status(self.pid)
            if s.rss_kb > self.peak.rss_kb:
                self.peak.rss_kb = s.rss_kb
            self.peak.threads = max(self.peak.threads, s.threads)

    def __enter__(self) -> "ResourceSampler":
        self.start_sample = _read_status(self.pid)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.end_sample = _read_status(self.pid)
        self._stop.set()
        self._thread.join(timeout=2)

    def report(self, wall_s: float) -> dict:
        cpu = max(0.0, self.end_sample.cpu_seconds - self.start_sample.cpu_seconds)
        return {
            "rss_mb": round(self.peak.rss_kb / 1024, 1),
            "cpu_seconds": round(cpu, 2),
            "cpu_percent_one_core": round(100 * cpu / wall_s, 1) if wall_s else 0.0,
            "threads": self.peak.threads,
        }


def percentile(values: list[float], p: float) -> float:
    """Ближайший ранг (nearest-rank); значения — миллисекунды."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(p / 100 * len(ordered)) - 1))
    return ordered[idx]


def latency_summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "n": len(values),
        "p50": round(percentile(values, 50), 1),
        "p95": round(percentile(values, 95), 1),
        "p99": round(percentile(values, 99), 1),
        "max": round(max(values), 1),
        "mean": round(statistics.fmean(values), 1),
    }


def write_json(name: str, payload: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_json(name: str) -> dict:
    return json.loads((RESULTS_DIR / name).read_text(encoding="utf-8"))


def setup_env(
    embedder: str = "hash", collection: str | None = None, db: str | None = None
) -> dict[str, str]:
    """Env, общий для provision и API-процесса. Вызывать ДО импорта app.*.

    Явные аргументы важнее os.environ: CLI-переопределения не должны затираться
    дефолтами. Имена проверяются allowlist'ом (без соединения).
    """
    db_name, coll = assert_safe(db, collection)
    env = os.environ.copy()
    env.update(
        {
            "CC_DATABASE_URL": db_url(db_name),
            "CC_QDRANT_URL": QDRANT_URL,
            "CC_QDRANT_COLLECTION": coll,
            # Явные имена целей: дочерние процессы (uvicorn, замеры) обязаны
            # резолвить ту же БД/коллекцию, что и provision.
            "CC_LOADTEST_DB": db_name,
            "CC_LOADTEST_COLLECTION": coll,
            "CC_EMBEDDER": embedder,
            "CC_ALLOW_DEMO_SECRETS": "true",
            "CC_JWT_SECRET": JWT_SECRET,
            "CC_ACCESS_TTL_MINUTES": "720",
            "CC_PUSH_ENABLED": "false",
            "CC_RATE_MAX": "100000",
            "CC_RATE_WINDOW": "60",
            "CC_ASSISTANT_LLM_URL": "",
            "CC_COOKIE_SECURE": "false",
            "PYTHONPATH": f"{API_DIR}:{CORE_DIR}",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


# --------------------------------------------------------------------------
# Замкнутый контур: C воркеров шлют запросы без пауз до дедлайна.
# --------------------------------------------------------------------------

@dataclass
class Scenario:
    name: str
    # fn(client, worker_id) -> число HTTP-запросов в транзакции (обычно 1)
    fn: Callable[[Any, int], Awaitable[int]]
    # настройка на воркер (например, свой токен/пациент)
    labels: dict = field(default_factory=dict)

@dataclass
class LoadResult:
    name: str
    concurrency: int
    duration_s: float
    flows: int
    requests: int
    errors: int
    status_counts: dict[int, int]
    error_types: dict[str, int]
    latencies_ms: list[float]
    resources: dict

    def summary(self) -> dict:
        err_rate = self.errors / self.requests if self.requests else 1.0
        return {
            "scenario": self.name,
            "concurrency": self.concurrency,
            "duration_s": round(self.duration_s, 2),
            "flows": self.flows,
            "requests": self.requests,
            "rps": round(self.requests / self.duration_s, 1),
            "flows_per_s": round(self.flows / self.duration_s, 1),
            "errors": self.errors,
            "error_rate": round(err_rate, 4),
            "status_counts": {str(k): v for k, v in sorted(self.status_counts.items())},
            "error_types": self.error_types,
            "latency_ms": latency_summary(self.latencies_ms),
            "resources": self.resources,
        }


async def _worker(
    scenario: Scenario,
    client: Any,
    worker_id: int,
    deadline: float,
    latencies: list[float],
    counters: dict[str, int],
    status_counts: dict[int, int],
    error_types: dict[str, int],
) -> None:
    while time.perf_counter() < deadline:
        t0 = time.perf_counter()
        try:
            n = await scenario.fn(client, worker_id)
            counters["requests"] += n
            counters["flows"] += 1
        except Exception as exc:  # noqa: BLE001 — любой сбой считаем ошибкой
            counters["errors"] += 1
            counters["requests"] += 1
            status = getattr(exc, "status_code", None)
            key = f"{type(exc).__name__}:{status}" if status else type(exc).__name__
            error_types[key] = error_types.get(key, 0) + 1
            if isinstance(status, int):
                status_counts[status] = status_counts.get(status, 0) + 1
        finally:
            latencies.append((time.perf_counter() - t0) * 1000)


async def run_closed_loop(
    scenario: Scenario,
    *,
    concurrency: int,
    duration_s: float,
    warmup_s: float = 1.0,
    pid: int | None = None,
) -> LoadResult:
    import httpx

    limits = httpx.Limits(
        max_connections=concurrency + 8,
        max_keepalive_connections=concurrency + 8,
    )
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(
        base_url=BASE_URL, limits=limits, timeout=timeout
    ) as client:
        # Прогрев соединений и пула БД, в статистику не входит.
        warm_deadline = time.perf_counter() + warmup_s
        for wid in range(concurrency):
            try:
                await asyncio.wait_for(scenario.fn(client, wid), timeout=10)
            except Exception:
                pass
            if time.perf_counter() >= warm_deadline:
                break

        latencies: list[float] = []
        counters: dict[str, int] = {"requests": 0, "flows": 0, "errors": 0}
        status_counts: dict[int, int] = {}
        error_types: dict[str, int] = {}
        sampler = ResourceSampler(pid) if pid else None
        if sampler is not None:
            sampler.__enter__()
        start = time.perf_counter()
        deadline = start + duration_s
        try:
            workers = [
                asyncio.create_task(
                    _worker(
                        scenario, client, wid, deadline, latencies,
                        counters, status_counts, error_types,
                    )
                )
                for wid in range(concurrency)
            ]
            await asyncio.gather(*workers)
            wall = time.perf_counter() - start
        finally:
            if sampler is not None:
                sampler.__exit__(None, None, None)

    resources = sampler.report(wall) if sampler else {}
    return LoadResult(
        name=scenario.name,
        concurrency=concurrency,
        duration_s=wall,
        flows=counters["flows"],
        requests=counters["requests"],
        errors=counters["errors"],
        status_counts=status_counts,
        error_types=error_types,
        latencies_ms=latencies,
        resources=resources,
    )



__all__ = [
    "ALLOW_ENV", "COLLECTION_PREFIX", "DB_PREFIX", "DEFAULT_COLLECTION", "DEFAULT_DB",
    "BASE_URL", "QDRANT_URL", "SafetyRefusal", "assert_destructive", "assert_safe",
    "apply_env", "describe_target", "db_url", "git_provenance", "redacted_db_url",
    "resolve_collection", "resolve_db", "setup_env",
    "API_DIR", "CORE_DIR", "ROOT", "RESULTS_DIR", "PYTHON", "JWT_SECRET", "PASSWORD",
    "PATIENTS", "CLINICIANS", "ResourceSampler", "Scenario", "run_closed_loop",
    "latency_summary", "percentile", "write_json", "read_json",
]
