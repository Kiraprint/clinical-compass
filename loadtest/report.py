"""Сборка loadtest/REPORT.md из результатов замеров."""

from __future__ import annotations

import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import common


def _load(name: str) -> dict:
    path = common.RESULTS_DIR / name
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _ms(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.1f}".replace(",", " ")


def _num(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}".replace(",", " ")


def _msk(value: str | None) -> str:
    if not value or value == "—":
        return "—"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M:%S MSK")


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine() or "—"


def _mem_total() -> str:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return f"{int(line.split()[1]) / 1024 / 1024:.1f} ГиБ"
    except OSError:
        pass
    return "—"


def _provenance_table(meta: dict, prov: dict, qdrant: dict) -> list[str]:
    git = meta.get("git") or common.git_provenance()
    dirty = git.get("dirty")
    commit = f"`{git.get('commit', '—')}`"
    if dirty is not None:
        commit += (
            f" (dirty: {git.get('changed_files', '?')} изменённых файлов)"
            if dirty
            else " (чистое дерево)"
        )
    llm_url = meta.get("llm_url", "")
    embedder = prov.get("embedder") or meta.get("embedder") or "—"
    device = qdrant.get("device", "—")
    collection = (
        qdrant.get("collection") or meta.get("collection") or common.DEFAULT_COLLECTION
    )
    return [
        "## 0. Провенанс и воспроизводимость",
        "",
        "| Что | Значение |",
        "|---|---|",
        f"| git commit (на момент замера) | {commit} |",
        f"| Дата прогона (MSK) | {_msk(meta.get('started_at'))} |",
        f"| Отчёт собран (MSK) | "
        f"{datetime.now(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d %H:%M:%S MSK')} |",
        f"| Python (API и стенд) | "
        f"{meta.get('host', {}).get('python') or platform.python_version()} |",
        f"| CPU | {_cpu_model()}, "
        f"{meta.get('host', {}).get('cpu_count') or os.cpu_count()} логических ядер |",
        f"| RAM | {_mem_total()} |",
        f"| ОС | {meta.get('host', {}).get('platform', '—')} |",
        f"| Эмбеддер | {embedder}, device={device}, "
        f"dense={qdrant.get('dense_size', '—')} |",
        f"| LLM | {'выключен' if not llm_url else 'включён: ' + str(llm_url)} "
        f"(`CC_ASSISTANT_LLM_URL=\"{llm_url}\"`) |",
        f"| БД | `{prov.get('database') or meta.get('database') or common.DEFAULT_DB}` |",
        f"| Коллекция Qdrant | `{collection}` |",
        f"| Чанков в индексе | {_num(qdrant.get('chunks'), 0)} |",
        f"| Документов (уникальных guideline_id) | {_num(qdrant.get('documents'), 0)} |",
        f"| Препаратов в справочнике | {_num(prov.get('drugs'), 0)} |",
        f"| Пользователей | {_num(prov.get('users'), 0)} "
        f"(пациентов {_num(prov.get('patients'), 0)}, "
        f"врачей {_num(prov.get('clinicians'), 0)}) |",
        "",
    ]


def _remeasure_block(meta: dict, prov: dict) -> list[str]:
    git = meta.get("git") or common.git_provenance()
    embedder = meta.get("embedder") or prov.get("embedder") or "hash"
    db = prov.get("database") or common.DEFAULT_DB
    collection = (
        prov.get("collection") or meta.get("collection") or common.DEFAULT_COLLECTION
    )
    commit = git.get("commit", "<commit из таблицы выше>")
    return [
        "### Как перемерить",
        "",
        "```bash",
        f"git checkout {commit}",
        "export CC_LOADTEST_ALLOW=1          # обязательно: provision делает DROP DATABASE",
        f"export CC_LOADTEST_DB={db}",
        f"export CC_LOADTEST_COLLECTION={collection}",
        "# 1) инфраструктура (Postgres + Qdrant демо-стека, порты 5432/6333)",
        "docker compose -f infra/docker-compose.yml up -d postgres qdrant",
        "# 2) безопасная проверка целей без соединений",
        "python -m loadtest.provision --dry-run",
        "# 3) пересоздать БД/коллекцию: схема, users, drugs, корпус + bench",
        f"python -m loadtest.provision --embedder {embedder}",
        "# 4) полный прогон (provision → API → ML → HTTP → ядро → REPORT.md)",
        f"python -m loadtest.run_all --embedder {embedder}",
        "# быстрый смоук: python -m loadtest.run_all --quick --embedder " + embedder,
        "```",
        "",
        "Требования: `bench/data/{queries,corpus}.jsonl` (см. `loadtest/README.md`), "
        "Python 3.12 с зависимостями API, `CC_LOADTEST_PYTHON` — интерпретатор для "
        "процесса API. Прогон детерминирован по данным (seed=42), но зависит от "
        "машины: числа сравнимы только на том же железе и том же коммите.",
        "",
        "Артефакты замера (`loadtest/results/*.json`, `loadtest/results/api.log`) "
        "в git не попадают (`.gitignore`), поэтому воспроизводимость обеспечивают "
        "эта команда и провенанс выше, а не сохранённые JSON.",
        "",
    ]


def build() -> Path:
    meta = _load("meta.json")
    core = _load("core.json")
    http = _load("http.json")
    ml = _load("ml.json")
    cliff = _load("cliff.json")

    lines: list[str] = []
    add = lines.append

    add("# Нагрузочные и ML-метрики Clinical Compass")
    add("")
    add(f"Дата прогона (MSK): {_msk(meta.get('started_at'))}.")
    add("")

    host = meta.get("host", {})
    prov = meta.get("provision", {})
    qdrant = prov.get("qdrant", {})
    server = meta.get("server", {})
    llm_url = meta.get("llm_url", "")
    collection = (
        qdrant.get("collection") or meta.get("collection") or common.DEFAULT_COLLECTION
    )

    add("Замеры выполнены на изолированном контуре: отдельная БД "
        f"`{prov.get('database') or meta.get('database') or common.DEFAULT_DB}`, "
        "отдельная коллекция Qdrant, отдельный процесс uvicorn. Демо- и прод-стек "
        "не затронуты.")
    add("")
    lines.extend(_provenance_table(meta, prov, qdrant))
    lines.extend(_remeasure_block(meta, prov))

    add("## 1. Стенд и методика")
    add("")
    add("| Параметр | Значение |")
    add("|---|---|")
    add(f"| CPU | {_cpu_model()} |")
    add(f"| Ядер | {host.get('cpu_count') or os.cpu_count()} |")
    add(f"| Python API | {host.get('python') or platform.python_version()} |")
    add(f"| Эмбеддер | {prov.get('embedder') or meta.get('embedder') or '—'}, "
        f"device={qdrant.get('device', '—')} (dense={qdrant.get('dense_size', '—')}) |")
    add(f"| Коллекция Qdrant | `{collection}` |")
    add(f"| Чанков в индексе | {_num(qdrant.get('chunks'), 0)} "
        f"(приложение {_num(qdrant.get('app_chunks'), 0)}, "
        f"золотой корпус {_num(qdrant.get('bench_chunks'), 0)}, "
        f"препараты {_num(qdrant.get('drug_chunks'), 0)}) |")
    add(f"| Пользователей | {_num(prov.get('users'), 0)} "
        f"(пациентов {_num(prov.get('patients'), 0)}, врачей {_num(prov.get('clinicians'), 0)}) |")
    add(f"| Холодный старт API | {_num(server.get('startup_s'), 2)} с |")
    add(f"| RSS после старта | {_num(server.get('idle_rss_mb'), 1)} МБ |")
    add(f"| LLM | {'выключен' if not llm_url else 'включён'} |")
    add("")
    add("Методика:")
    duration = http.get("duration_s")
    add("- HTTP: замкнутый контур, C воркеров без пауз"
        + (f", длительность {_num(duration, 1)} с на уровень" if duration else "")
        + "; прогрев 1 с не учитывается.")
    add("- max RPS — лучший уровень конкуренции с долей ошибок < 1 %.")
    add("- Латентность — по каждой HTTP-операции; p50/p95/p99 (nearest-rank).")
    add("- RPS сценария-транзакции (screening flow) считается по транзакциям, "
        "запросов в транзакции — 3.")
    add("- LLM отключён (`CC_ASSISTANT_LLM_URL=\"\"`): измеряется детерминированный "
        "RAG-путь, без внешней модели.")
    add("")

    if core:
        add("## 2. Ядро (одно ядро)")
        add("")
        add("| Функция | ops/s | p50, мкс | p95, мкс | Сэмплы (вызовов × повторов) |")
        add("|---|---:|---:|---:|---|")
        for name, row in core.get("functions", {}).items():
            samples = f"{_num(row.get('n'), 0)} × {row.get('repeats', '—')}"
            add(f"| `{name}` | {_num(row['ops_per_s'], 0)} | "
                f"{row['us_p50']:.1f} | {row['us_p95']:.1f} | {samples} |")
        agent_core = core.get("agent_path", {})
        if agent_core.get("status") == "ok":
            samples = f"{_num(agent_core.get('n'), 0)} × {agent_core.get('repeats', '—')}"
            add(f"| `agent.AgentFactory.invoke` (поиск Qdrant + ответ, LLM off) | "
                f"{_num(agent_core.get('ops_per_s'), 0)} | "
                f"{agent_core.get('us_p50', 0):.1f} | "
                f"{agent_core.get('us_p95', 0):.1f} | {samples} |")
        elif agent_core:
            add("")
            add(f"> Замер пути агента пропущен: {agent_core.get('reason', '—')}")
        add("")
        scaling = core.get("scaling_ops_per_s_by_processes", {})
        if scaling:
            add("Масштабирование по процессам, ops/s (см. `run_core.scalable`: "
                "40 000 вызовов на точку, у `depersonalize` — 10 000):")
            add("")
            add("| Функция | 1 | 2 | 4 | 8 |")
            add("|---|---:|---:|---:|---:|")
            for name, row in scaling.items():
                add(f"| `{name}` | " + " | ".join(
                    _num(row.get(str(k)), 0) for k in (1, 2, 4, 8)
                ) + " |")
            add("")

    if ml:
        direct = ml.get("direct", {})
        agent = ml.get("agent", {})
        dm, am = direct.get("metrics", {}), agent.get("metrics", {})
        add("## 3. ML-метрики поиска")
        add("")
        add(f"Золотой набор: {_num(dm.get('queries'), 0)} запросов RU+EN, "
            "qrels — чанки корпуса bench. "
            "Обе оценки на одном индексе и одном эмбеддере.")
        add("")
        add("| Метрика | Прямой `hybrid_search` | Агент `POST /assistant/ask` |")
        add("|---|---:|---:|")
        labels = [
            ("recall@1", "Recall@1"),
            ("recall@3", "Recall@3"),
            ("recall@5", "Recall@5"),
            ("ndcg@3", "nDCG@3"),
            ("ndcg@10", "nDCG@10"),
            ("mrr@3", "MRR@3"),
            ("mrr@10", "MRR@10"),
            ("hit@3", "Hit@3 (есть релевантный в топ-3)"),
        ]

        for key, label in labels:
            add(f"| {label} | {dm.get(key, 0):.3f} | {am.get(key, 0):.3f} |")
        add("")
        add("> Агент отдаёт не более 3 цитат (`build_rag_answer(max_citations=3)`), "
            "поэтому для него метрики @5/@10 равны @3; сравнение корректно на @3.")
        add("")

        add("| Метрика | Прямой поиск | Агент |")
        add("|---|---:|---:|")
        add(f"| Запросов в срезе | {_num(dm.get('queries'), 0)} | "
            f"{_num(am.get('queries'), 0)} |")
        add(f"| Латентность p50, мс | {_ms(direct.get('latency_ms', {}).get('p50'))} | "
            f"{_ms(agent.get('latency_ms', {}).get('p50'))} |")
        add(f"| Латентность p95, мс | {_ms(direct.get('latency_ms', {}).get('p95'))} | "
            f"{_ms(agent.get('latency_ms', {}).get('p95'))} |")
        add(f"| Среднее число цитат | — | {_num(agent.get('citations_mean'), 2)} |")
        add(f"| Ответов с опорой (grounded) | — | {agent.get('grounded_rate', 0):.1%} |")
        add(f"| Отказов (refused) | — | {agent.get('refused_rate', 0):.1%} |")
        red = agent.get("redline", {})
        add(f"| Red line: отказов на диагноз/назначение | — | "
            f"{red.get('refused_ok', 0)}/{red.get('queries', 0)} "
            f"({red.get('accuracy', 0):.0%}) |")
        add("")
        if red.get("results"):
            add("| Red-line запрос | Отказ | Код |")
            add("|---|---|---|")
            for row in red["results"]:
                mark = "да" if row.get("refused") else "**НЕТ**"
                add(f"| {row['query']} | {mark} | {row.get('refusal_code') or '—'} |")
            add("")
        if direct.get("by_lang"):
            add("По языкам запросов (сравнимый срез @3):")
            add("")
            add("| Язык | Прямой nDCG@3 | Прямой R@3 | Агент nDCG@3 | Агент R@3 |")
            add("|---|---:|---:|---:|---:|")
            agent_lang = agent.get("by_lang", {})
            for lang, row in direct["by_lang"].items():
                arow = agent_lang.get(lang, {})
                add(f"| {lang} | {row.get('ndcg@3', 0):.3f} | {row.get('recall@3', 0):.3f} | "
                    f"{arow.get('ndcg@3', 0):.3f} | {arow.get('recall@3', 0):.3f} |")
            add("")
        worst = sorted(
            agent.get("per_query", []),
            key=lambda row: (row.get("ndcg@3", 0), row.get("recall@3", 0)),
        )[:5]
        if worst:
            add("Худшие 5 запросов агента по nDCG@3:")
            add("")
            add("| Запрос | nDCG@3 | R@3 | Цитат |")
            add("|---|---:|---:|---:|")
            for row in worst:
                add(f"| `{row['id']}` | {row.get('ndcg@3', 0):.3f} | "
                    f"{row.get('recall@3', 0):.3f} | {row.get('citations', 0)} |")
            add("")

    if http:
        add("## 4. HTTP-нагрузка: сводка")
        add("")
        add("| Сценарий | max RPS | flows/s | C | p50, мс | p95, мс | p99, мс | Ошибки | "
            "1 соединение, RPS | RSS, МБ | CPU, % ядра |")
        add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        scenarios = http.get("scenarios", {})
        for key, block in scenarios.items():
            s = block["summary"]
            lat = s.get("latency_at_max") or {}
            err_rate = 0.0
            flows_rate = None
            for point in s["points"]:
                if point["concurrency"] == s.get("max_rps_concurrency"):
                    err_rate = point["error_rate"]
                    flows_rate = point["flows_per_s"]
            add(f"| {block['name']} | {_num(s['max_rps'])} | {_num(flows_rate)} | "
                f"{s.get('max_rps_concurrency') or '—'} | {_ms(lat.get('p50'))} | "
                f"{_ms(lat.get('p95'))} | {_ms(lat.get('p99'))} | {err_rate:.2%} | "
                f"{_num(s['single_connection_rps'])} | {_num(s['peak_rss_mb'])} | "
                f"{_num(s['peak_cpu_percent_one_core'])} |")
        add("")
        add("### Кривые по конкуренции")
        add("")
        add("| Сценарий | C | RPS | Запросов | p50, мс | p95, мс | p99, мс | Ошибки |")
        add("|---|---:|---:|---:|---:|---:|---:|---:|")
        for block in scenarios.values():
            for point in block["summary"]["points"]:
                add(f"| {block['name']} | {point['concurrency']} | {_num(point['rps'])} | "
                    f"{_num(point.get('requests'), 0)} | "
                    f"{_ms(point['latency_ms']['p50'])} | {_ms(point['latency_ms']['p95'])} | "
                    f"{_ms(point['latency_ms']['p99'])} | {point['error_rate']:.2%} |")
        add("")
        if cliff.get("probes"):
            add("### Предел пула соединений (чтение дневника, свежий процесс на точку)")
            add("")
            add("| Пул | C | RPS | p50, мс | Ошибки | Типы ошибок |")
            add("|---:|---:|---:|---:|---:|---|")
            for point in cliff["probes"]:
                kinds = ", ".join(
                    f"{name}×{count}" for name, count in point.get("error_types", {}).items()
                ) or "—"
                add(f"| {point.get('pool_size', 15)} | {point['concurrency']} | "
                    f"{_num(point['rps'])} | {_ms(point['latency_ms']['p50'])} | "
                    f"{point['error_rate']:.2%} | {kinds} |")
            add("")

            add("> `pool_size=80` даёт 80+20 = 100 соединений — ровно "
                "`max_connections=100` без запаса, поэтому C≥96 упирается в лимит "
                "Postgres, а не в приложение.")
            add("")
        low_concurrency = [
            point
            for block in scenarios.values()
            for point in block["summary"]["points"]
            if point["concurrency"] <= 48
        ]
        if low_concurrency:
            worst_low = max(point["error_rate"] for point in low_concurrency)
            add(f"До C=48 включительно максимальная доля ошибок по всем сценариям — "
                f"{worst_low:.2%}.")
        add("")

        by_key = {key: block["summary"] for key, block in scenarios.items()}
        health_rps = by_key.get("health", {}).get("max_rps", 0)
        db_rps = by_key.get("diary_read", {}).get("max_rps", 0)
        write_rps = by_key.get("diary_write", {}).get("max_rps", 0)
        ask_rps = by_key.get("clinician_ask", {}).get("max_rps", 0)
        flow = by_key.get("screening_flow", {})
        flow_rate = 0.0
        for point in flow.get("points", []):
            if point["concurrency"] == flow.get("max_rps_concurrency"):
                flow_rate = point["flows_per_s"]
        add("## 5. Выводы")
        add("")
        if health_rps:
            add(f"- Предел одного процесса uvicorn без БД: **{_num(health_rps)} RPS**.")
        if db_rps:
            share = 100 * db_rps / health_rps if health_rps else 0
            add(f"- Чтение дневника (JWT + PostgreSQL + аудит): **{_num(db_rps)} RPS** — "
                f"{share:.0f} % от предела без БД.")
        if write_rps:
            add(f"- Запись дневника (шифрование + commit + аудит): **{_num(write_rps)} RPS**.")
        if flow_rate:
            add(f"- Полный скрининг PHQ-9 (3 HTTP-запроса, 9 ответов, запись + аудит): "
                f"**{_num(flow_rate)} транзакций/с**.")
        if cliff.get("probes"):
            probes = sorted(cliff["probes"], key=lambda p: (p.get("pool_size", 0), p["concurrency"]))
            default_pool = min(p.get("pool_size", 0) for p in probes)
            default = [p for p in probes if p.get("pool_size") == default_pool]
            broken = [p for p in default if p["error_rate"] > 0.01]
            held = [p for p in default if p["error_rate"] <= 0.01]
            if broken:
                add(f"- Async-пул по умолчанию ({default_pool} соединений) обрывается при "
                    f"**C≥{broken[0]['concurrency']}**: {broken[0]['error_rate']:.0%} ошибок, "
                    f"p50 {_ms(broken[0]['latency_ms']['p50'])} мс.")
            if held:
                peak = max(held, key=lambda p: p["concurrency"])
                add(f"- Async-пул по умолчанию ({default_pool} соединений) держит "
                    f"**C={peak['concurrency']}** без ошибок при {_num(peak['rps'])} RPS "
                    f"(p50 {_ms(peak['latency_ms']['p50'])} мс).")
            enlarged = [p for p in probes if p.get("pool_size", 0) > default_pool]
            peak_fixed = [p for p in enlarged if p["error_rate"] <= 0.01]
            if peak_fixed:
                peak = max(peak_fixed, key=lambda p: p["concurrency"])
                add(f"- С пулом {peak['pool_size']} соединений тот же сценарий держит "
                    f"**C={peak['concurrency']}** без ошибок при {_num(peak['rps'])} RPS.")
            limited = [p for p in enlarged if p["error_rate"] > 0.01]
            if limited:
                worst = max(limited, key=lambda p: p["concurrency"])
                add(f"- Ошибки на пуле {worst['pool_size']} при C={worst['concurrency']} — "
                    f"похоже на упор в `max_connections=100` Postgres (пул 80+20 "
                    f"не оставляет запаса), а не дефект приложения.")
        if ask_rps:
            add(f"- RAG-ответ врачу (Qdrant + аудит, детерминированная сборка): "
                f"**{_num(ask_rps)} RPS**; время ответа p50 "
                f"{_ms((by_key['clinician_ask'].get('latency_at_max') or {}).get('p50'))} / "
                f"p95 {_ms((by_key['clinician_ask'].get('latency_at_max') or {}).get('p95'))} мс "
                f"на C={by_key['clinician_ask'].get('max_rps_concurrency')}.")
        roster = by_key.get("clinician_roster", {})
        if roster.get("max_rps"):
            lat = roster.get("latency_at_max") or {}
            add(f"- Роспись врача: **{_num(roster['max_rps'])} RPS**, p50 "
                f"{_ms(lat.get('p50'))} мс — батчинг из 4 запросов на весь список "
                f"(было 1+3N запросов на пациента).")
        if ml:
            direct_p50 = ml.get("direct", {}).get("latency_ms", {}).get("p50")
            agent_p50 = ml.get("agent", {}).get("latency_ms", {}).get("p50")
            dm = ml.get("direct", {}).get("metrics", {})
            am = ml.get("agent", {}).get("metrics", {})
            red = ml.get("agent", {}).get("redline", {})
            if direct_p50 and agent_p50:
                add(f"- Накладные расходы агента над чистым поиском: "
                    f"{_num(agent_p50 - direct_p50)} мс на запрос "
                    f"(p50 {_num(direct_p50)} → {_num(agent_p50)} мс).")
            add(f"- ML ({meta.get('embedder', '—')}-эмбеддер): Recall@3 " 
                f"{dm.get('recall@3', 0):.3f} → "
                f"{am.get('recall@3', 0):.3f}, nDCG@3 {dm.get('ndcg@3', 0):.3f} → "
                f"{am.get('ndcg@3', 0):.3f}, MRR@3 {dm.get('mrr@3', 0):.3f} → "
                f"{am.get('mrr@3', 0):.3f} (прямой поиск → агент).")
            add(f"- Red line: {red.get('refused_ok', 0)}/{red.get('queries', 0)} "
                f"отказов на запросы диагноза/назначения "
                f"({red.get('accuracy', 0):.0%}).")
            missed = [row["query"] for row in red.get("results", []) if not row.get("refused")]
            if missed:
                add(f"- **Red line пропускает**: {'; '.join(missed)}.")
        corpus = core.get("functions", {}).get("kb_seed.build_corpus (кеш TTL-снимка)")
        if corpus:
            add(f"- `build_corpus` по кешу: {_num(corpus['ops_per_s'], 0)} вызовов в "
                f"секунду (p50 {_num(corpus['us_p50'], 1)} мкс) — это попадание в "
                f"TTL-снимок статических секций; препараты в снимке читаются из БД "
                f"один раз на сборку. Реальный путь агента (поиск + ответ) — строка "
                f"`agent.AgentFactory.invoke` в разделе 2.")
        add("")
        add("### 5.1 Сравнение с прошлыми прогонами")
        add("")
        add("Исторические числа этого отчёта (базовый прогон 2026-10-23, отдельный "
            "замер bge-m3 на RTX 5090) удалены: у них не был зафиксирован git-коммит, "
            "а `loadtest/results/*.json` в git не хранятся — воспроизвести их нельзя. "
            "Сравнивать можно только прогоны, собранные этой версией `report.py` "
            "(провенанс — раздел 0): выполните «Как перемерить» на двух коммитах и "
            "сопоставьте таблицы.")
        add("")
        add("## 6. Ограничения")
        add("")
        add("- Один процесс API, без nginx/TLS и без внешнего LLM; `WEB_CONCURRENCY` "
            "поддержан, но в этом прогоне не измерялся.")
        add("- Postgres и Qdrant — локально в Docker; межсерверная сеть не моделируется.")
        add("- Эмбеддер `hash` — демо-база; bge-m3 включается `CC_EMBEDDER=bge-m3` "
            "с extra `embeddings` и переиндексацией KB.")
        add("- Замкнутый контур: p95 на высоких C включает очередь к async-пулу БД "
            "(SQLAlchemy: 20 + 20 overflow на процесс).")
        add("")

    report_path = common.HERE / "REPORT.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


if __name__ == "__main__":
    print(build())
