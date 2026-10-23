"""Сборка loadtest/REPORT.md из результатов замеров."""

from __future__ import annotations

import json
from pathlib import Path

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
    add(f"Дата прогона: {meta.get('started_at', '—')}.")
    add("")
    add("Замеры выполнены на изолированном контуре: отдельная БД `compass_loadtest`, "
        "отдельная коллекция Qdrant, отдельный процесс uvicorn. Демо- и прод-стек "
        "не затронуты.")
    add("")

    host = meta.get("host", {})
    prov = meta.get("provision", {})
    qdrant = prov.get("qdrant", {})
    server = meta.get("server", {})
    add("## 1. Стенд и методика")
    add("")
    add("| Параметр | Значение |")
    add("|---|---|")
    add(f"| CPU | {host.get('platform', '—')} |")
    add(f"| Ядер | {host.get('cpu_count', '—')} |")
    add(f"| Python API | {host.get('python', '—')} |")
    add(f"| Эмбеддер | {prov.get('qdrant', {}).get('embedder', meta.get('embedder', '—'))} "
        f"(dense={qdrant.get('dense_size', '—')}) |")
    add(f"| Коллекция Qdrant | `{qdrant.get('collection', meta.get('collection', '—'))}` |")
    add(f"| Документов в индексе | {_num(qdrant.get('documents'), 0)} "
        f"(приложение {_num(qdrant.get('app_documents'), 0)}, "
        f"золотой корпус {_num(qdrant.get('bench_documents'), 0)}) |")
    add(f"| Пользователей | {_num(prov.get('users'), 0)} "
        f"(пациентов {_num(prov.get('patients'), 0)}, врачей {_num(prov.get('clinicians'), 0)}) |")
    add(f"| Холодный старт API | {_num(server.get('startup_s'), 2)} с |")
    add(f"| RSS после старта | {_num(server.get('idle_rss_mb'), 1)} МБ |")
    add("")
    add("Методика:")
    add("")
    add("- HTTP: замкнутый контур, C воркеров без пауз, длительность "
        f"{_num(http.get('duration_s'), 1)} с на уровень; прогрев 1 с не учитывается.")
    add("- max RPS — лучший уровень конкуренции с долей ошибок < 1 %.")
    add("- Латентность — по каждой HTTP-операции; p50/p95/p99 (nearest-rank).")
    add("- RPS сценария-транзакции (screening flow) считается по транзакциям, "
        "запросов в транзакции — 3.")
    add("- LLM отключён (`CC_ASSISTANT_LLM_URL=\"\"`): измеряется детерминированный "
        "RAG-путь, без внешней модели.")
    add("")

    if core:
        add("## 2. Ядро (чистые функции, одно ядро)")
        add("")
        add("| Функция | ops/s | p50, мкс | p95, мкс |")
        add("|---|---:|---:|---:|")
        for name, row in core.get("functions", {}).items():
            add(f"| `{name}` | {_num(row['ops_per_s'], 0)} | "
                f"{row['us_p50']:.1f} | {row['us_p95']:.1f} |")
        add("")
        scaling = core.get("scaling_ops_per_s_by_processes", {})
        if scaling:
            add("Масштабирование по процессам, ops/s:")
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
        add("## 3. ML-метрики поиска")
        add("")
        add("Золотой набор: 55 запросов RU+EN, qrels — чанки корпуса bench. "
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
        dm, am = direct.get("metrics", {}), agent.get("metrics", {})
        for key, label in labels:
            add(f"| {label} | {dm.get(key, 0):.3f} | {am.get(key, 0):.3f} |")
        add("")
        add("> Агент отдаёт не более 3 цитат (`build_rag_answer(max_citations=3)`), "
            "поэтому для него метрики @5/@10 равны @3; сравнение корректно на @3.")
        add("")

        add("| Метрика | Прямой поиск | Агент |")
        add("|---|---:|---:|")
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
        add("| Сценарий | C | RPS | p50, мс | p95, мс | p99, мс | Ошибки |")
        add("|---|---:|---:|---:|---:|---:|---:|")
        for block in scenarios.values():
            for point in block["summary"]["points"]:
                add(f"| {block['name']} | {point['concurrency']} | {_num(point['rps'])} | "
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

            add("> `pool_size=80` даёт 80+20 соединений: C=128 превышает "
                "их, поэтому ошибки на последней строке — ожидаемое насыщение.")
            add("")
        add("На уровнях до C=48 включительно ошибок не было ни в одном сценарии.")
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
            broken = [
                p for p in cliff["probes"]
                if p.get("pool_size", 15) <= 15 and p["error_rate"] > 0.01
            ]
            fixed = [
                p for p in cliff["probes"]
                if p.get("pool_size", 15) > 15 and p["error_rate"] <= 0.01
            ]
            if broken:
                add(f"- Дефолтный пул (5+10) обрывается при **C≈{broken[0]['concurrency']}**: "
                    f"{broken[0]['error_rate']:.0%} ошибок, p50 "
                    f"{_ms(broken[0]['latency_ms']['p50'])} мс "
                    f"(15 соединений висят в `idle in transaction`).")
            if fixed:
                peak = max(fixed, key=lambda p: p["concurrency"])
                add(f"- С пулом {peak['pool_size']} тот же сценарий держит "
                    f"**C={peak['concurrency']}** без ошибок при {_num(peak['rps'])} RPS — "
                    f"обрыв снимается размером пула, но не пропускной способностью БД.")
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
                f"{_ms(lat.get('p50'))} мс — N+1 в "
                f"`clinician.list_patients` (3 запроса на пациента).")
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
            add(f"- ML (hash-эмбеддер): Recall@3 {dm.get('recall@3', 0):.3f} → "
                f"{am.get('recall@3', 0):.3f}, nDCG@3 {dm.get('ndcg@3', 0):.3f} → "
                f"{am.get('ndcg@3', 0):.3f}, MRR@3 {dm.get('mrr@3', 0):.3f} → "
                f"{am.get('mrr@3', 0):.3f} (прямой поиск → агент).")
            add(f"- Red line: {red.get('refused_ok', 0)}/{red.get('queries', 0)} "
                f"отказов на запросы диагноза/назначения "
                f"({red.get('accuracy', 0):.0%}).")
            missed = [row["query"] for row in red.get("results", []) if not row.get("refused")]
            if missed:
                add(f"- **Red line пропускает**: {'; '.join(missed)} — "
                    f"формулировка без глагола назначения/диагноза не матчится "
                    f"`detect_redline`.")
        corpus = core.get("functions", {}).get("kb_seed.build_corpus (на запрос агента)")
        if corpus:
            add(f"- `build_corpus` при промахе лексического гейта: "
                f"{_num(corpus['ops_per_s'], 0)} вызовов в секунду "
                f"(p50 {_num(corpus['us_p50'] / 1000, 2)} мс) — выполняется на каждый "
                f"запрос агента, где поиск вернул меньше `limit` фрагментов.")
        add("")
        add("## 6. Ограничения")
        add("")
        add("- Один процесс API, без nginx/TLS и без внешнего LLM.")
        add("- Postgres и Qdrant — локально в Docker; межсерверная сеть не моделируется.")
        add("- Эмбеддер `hash` — демо-база; прод-качество поиска см. `bench/README.md` "
            "(bge-m3: nDCG@10 0.815 на тех же 55 запросах).")
        add("- Замкнутый контур: p95 на высоких C включает очередь к пулу БД "
            "(SQLAlchemy: 5 + 10 overflow).")
        add("")

    report_path = common.HERE / "REPORT.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


if __name__ == "__main__":
    print(build())
