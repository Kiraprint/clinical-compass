"""HTTP-нагрузочные сценарии: замкнутый контур по уровням конкуренции.

Каждый уровень: фиксированная длительность, C воркеров без пауз, замер
латентности каждой операции. max RPS = лучший уровень с error rate < 1%.
"""

from __future__ import annotations

import asyncio
import json
import time

from . import common, serve

PHQ_ANSWERS = {"q1": 2, "q2": 2, "q3": 1, "q4": 2, "q5": 1, "q6": 1, "q7": 2, "q8": 1, "q9": 0}
QUERIES: list[str] = []


def _load_queries() -> list[str]:
    if QUERIES:
        return QUERIES
    path = common.bench_data_dir() / "queries.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            QUERIES.append(json.loads(line)["q"])
    return QUERIES


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}

class HttpError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"HTTP {status_code}: {body[:200]}")
        self.status_code = status_code


async def _check(response, expected: int | tuple[int, ...] = (200, 201, 204)) -> None:
    if response.status_code not in (expected if isinstance(expected, tuple) else (expected,)):
        raise HttpError(response.status_code, response.text)


def token_pool(credentials: dict, role: str) -> list[str]:
    return [row["token"] for row in credentials[role]]


def screen_flow(credentials: dict):
    patients = token_pool(credentials, "patients")
    counters = {"n": 0}

    async def run(client, wid: int) -> int:
        headers = _auth(patients[wid % len(patients)])
        counters["n"] += 1
        response = await client.post("/api/v1/screenings", json={}, headers=headers)
        if response.status_code == 409:
            await _check(
                await client.post("/api/v1/screenings/abandon", headers=headers), 200
            )
            response = await client.post("/api/v1/screenings", json={}, headers=headers)
        await _check(response, 201)
        run_id = response.json()["run_id"]
        await _check(
            await client.post(
                f"/api/v1/runs/{run_id}/answers",
                json={"answers": PHQ_ANSWERS},
                headers=headers,
            ),
            204,
        )
        await _check(
            await client.post(f"/api/v1/runs/{run_id}/finish", headers=headers), 201
        )
        return 3

    return run


def ask(credentials: dict):
    clinicians = token_pool(credentials, "clinicians")
    queries = _load_queries()

    async def run(client, wid: int) -> int:
        headers = _auth(clinicians[wid % len(clinicians)])
        query = queries[(wid * 7 + int(time.time() * 1000) // 1000) % len(queries)]
        await _check(
            await client.post(
                "/api/v1/assistant/ask",
                json={"query": query, "limit": 5},
                headers=headers,
            )
        )
        return 1

    return run


def read_diary(credentials: dict):
    patients = token_pool(credentials, "patients")

    async def run(client, wid: int) -> int:
        await _check(
            await client.get("/api/v1/diary", headers=_auth(patients[wid % len(patients)]))
        )
        return 1

    return run


def write_diary(credentials: dict):
    patients = token_pool(credentials, "patients")
    seq = {"n": 0}

    async def run(client, wid: int) -> int:
        seq["n"] += 1
        await _check(
            await client.post(
                "/api/v1/diary",
                json={
                    "content": f"Нагрузочная запись {seq['n']}: настроение ровное, сон 7 ч.",
                    "mood": 3,
                    "sleep_h": 7.0,
                    "tags": ["нагрузка"],
                },
                headers=_auth(patients[wid % len(patients)]),
            ),
            201,
        )
        return 1

    return run


def checkin_today(credentials: dict):
    patients = token_pool(credentials, "patients")

    async def run(client, wid: int) -> int:
        await _check(
            await client.get(
                "/api/v1/checkins/today", headers=_auth(patients[wid % len(patients)])
            )
        )
        return 1

    return run


def questionnaires(credentials: dict):
    patients = token_pool(credentials, "patients")

    async def run(client, wid: int) -> int:
        await _check(
            await client.get(
                "/api/v1/questionnaires", headers=_auth(patients[wid % len(patients)])
            )
        )
        return 1

    return run


def clinician_patients(credentials: dict):
    clinicians = token_pool(credentials, "clinicians")

    async def run(client, wid: int) -> int:
        await _check(
            await client.get(
                "/api/v1/clinician/patients",
                headers=_auth(clinicians[wid % len(clinicians)]),
            )
        )
        return 1

    return run


def mixed(credentials: dict):
    """Реалистичный профиль: чтения/записи пациента + RAG врача."""
    patients = token_pool(credentials, "patients")
    clinicians = token_pool(credentials, "clinicians")
    queries = _load_queries()
    flow = screen_flow(credentials)
    seq = {"n": 0}

    async def run(client, wid: int) -> int:
        seq["n"] += 1
        bucket = (wid * 7919 + seq["n"] * 104729) % 100
        headers = _auth(patients[wid % len(patients)])
        if bucket < 30:
            await _check(await client.get("/api/v1/diary", headers=headers))
            return 1
        if bucket < 45:
            await _check(await client.get("/api/v1/checkins/today", headers=headers))
            return 1
        if bucket < 57:
            await _check(await client.get("/api/v1/questionnaires", headers=headers))
            return 1
        if bucket < 70:
            await _check(
                await client.post(
                    "/api/v1/diary",
                    json={
                        "content": f"Смешанный профиль {seq['n']}: тревога 4/10.",
                        "mood": 2,
                        "sleep_h": 6.5,
                        "tags": ["mixed"],
                    },
                    headers=headers,
                ),
                201,
            )
            return 1
        if bucket < 82:
            await _check(
                await client.get(
                    "/api/v1/me/prescribed-scales", headers=headers
                )
            )
            return 1
        if bucket < 92:
            await _check(
                await client.post(
                    "/api/v1/assistant/ask",
                    json={
                        "query": queries[(wid * 13 + seq["n"] * 31) % len(queries)],
                        "limit": 5,
                    },
                    headers=_auth(clinicians[wid % len(clinicians)]),
                )
            )
            return 1
        if bucket < 97:
            await _check(
                await client.get(
                    "/api/v1/clinician/patients",
                    headers=_auth(clinicians[wid % len(clinicians)]),
                )
            )
            return 1
        return await flow(client, wid)

    return run


def build_scenarios(credentials: dict) -> dict[str, common.Scenario]:
    async def health(client, wid: int) -> int:
        await _check(await client.get("/health"))
        return 1

    async def auth_config(client, wid: int) -> int:
        await _check(await client.get("/api/v1/auth/config"))
        return 1

    return {
        "health": common.Scenario("health (без БД)", health),
        "auth_config": common.Scenario("auth/config (без БД/аутентификации)", auth_config),
        "diary_read": common.Scenario("diary read (JWT+БД+аудит)", read_diary(credentials)),
        "diary_write": common.Scenario("diary write (шифрование+БД)", write_diary(credentials)),
        "checkin_today": common.Scenario("checkin today (ядро+БД)", checkin_today(credentials)),
        "screening_flow": common.Scenario(
            "screening flow (3 запроса, запись+аудит)", screen_flow(credentials)
        ),
        "clinician_ask": common.Scenario(
            "assistant/ask (RAG: Qdrant+аудит)", ask(credentials)
        ),
        "clinician_roster": common.Scenario(
            "clinician patients (roster)", clinician_patients(credentials)
        ),
        "mixed": common.Scenario("mixed (пациент+врач)", mixed(credentials)),
    }


def summarize(points: list[dict]) -> dict:
    stable = [p for p in points if p["error_rate"] < 0.01]
    best = max(stable, key=lambda p: p["rps"]) if stable else None
    base = min(points, key=lambda p: p["concurrency"])
    return {
        "points": points,
        "max_rps": best["rps"] if best else 0.0,
        "max_rps_concurrency": best["concurrency"] if best else None,
        "latency_at_max": best["latency_ms"] if best else None,
        "single_connection_rps": base["rps"],
        "speedup": round((best["rps"] / base["rps"]), 2) if best and base["rps"] else 0.0,
        "peak_rss_mb": max((p["resources"].get("rss_mb", 0) for p in points), default=0),
        "peak_cpu_percent_one_core": max(
            (p["resources"].get("cpu_percent_one_core", 0) for p in points), default=0
        ),
    }


def run(
    credentials: dict,
    *,
    pid: int | None = None,
    quick: bool = False,
    concurrency_levels: list[int] | None = None,
    duration_s: float | None = None,
) -> dict:
    scenarios = build_scenarios(credentials)
    levels = concurrency_levels or ([1, 8, 32] if quick else [1, 4, 8, 16, 32, 48])
    duration = duration_s or (5.0 if quick else 8.0)
    output: dict = {"duration_s": duration, "scenarios": {}}

    for key, scenario in scenarios.items():
        points: list[dict] = []
        for c in levels:
            result = asyncio.run(
                common.run_closed_loop(
                    scenario,
                    concurrency=c,
                    duration_s=duration,
                    warmup_s=1.0,
                    pid=pid,
                )
            )
            point = result.summary()
            points.append(point)
            print(
                f"{key:18s} C={c:3d}  rps={point['rps']:8.1f}  "
                f"p50={point['latency_ms']['p50']:7.1f}ms  "
                f"p95={point['latency_ms']['p95']:7.1f}ms  "
                f"err={point['error_rate']:.2%}"
            )
        output["scenarios"][key] = {
            "name": scenario.name,
            "summary": summarize(points),
        }

    common.write_json("http.json", output)
    return output




def run_cliff(credentials: dict, *, quick: bool = False, embedder: str = "hash",
              collection: str = common.QDRANT_COLLECTION) -> dict:
    """Проверка обрыва на высокой конкуренции: дефолтный пул и увеличенный.

    Каждый уровень — на свежем процессе API, чтобы исключить влияние
    предыдущего уровня на пул соединений.
    """
    scenario = build_scenarios(credentials)["diary_read"]
    plan = (
        [(0, 56), (0, 60), (80, 96)]
        if quick
        else [(0, 56), (0, 60), (0, 64), (80, 64), (80, 96), (80, 128)]
    )
    results = []
    for pool_size, concurrency in plan:
        server = serve.ApiServer(embedder=embedder, collection=collection,
                                 pool_size=pool_size)
        server.start()
        try:
            result = asyncio.run(
                common.run_closed_loop(
                    scenario, concurrency=concurrency, duration_s=5.0,
                    warmup_s=0.5, pid=server.proc.pid,
                )
            )
            point = result.summary()
        finally:
            server.stop()
        point["pool_size"] = pool_size or 15
        results.append(point)
        print(
            f"cliff pool={pool_size or 15:3d} C={concurrency:3d}  "
            f"rps={point['rps']:8.1f}  p50={point['latency_ms']['p50']:8.1f}ms  "
            f"err={point['error_rate']:.2%}"
        )
    payload = {"probes": results}
    common.write_json("cliff.json", payload)
    return payload
if __name__ == "__main__":
    credentials = common.read_json("credentials.json")
    print(json.dumps(run(credentials), ensure_ascii=False, indent=2))
