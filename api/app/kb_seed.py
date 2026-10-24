"""Сбор базы знаний ИИ-агента в Qdrant (docs/08 §5). Офлайн-демо.

CLI: python -m app.kb_seed [--url CC_QDRANT_URL] [--reset]

Корпус строится из СОБСТВЕННОГО контента движка (docs/08 §2): описания шкал,
правила триажа, красные линии. Только открытый не-ПДн материал (docs/07 §3.5).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from qdrant_client import QdrantClient
from sqlalchemy import select

from app.config import settings
from app.services import kb
from app.services.embeddings import build_embedder

from compass.scales import ALL_SCALES
from compass.triage import triage
from compass.models import Severity, Triage


def _scale_docs() -> list[dict]:
    docs: list[dict] = []
    for scale in ALL_SCALES.values():
        items = "; ".join(f"{it.key}: {it.text}" for it in scale.items)
        thresholds = " ".join(f"сумма {bound} → {sev}" for bound, sev in scale.severity_bounds)
        docs.append(
            {
                "guideline_id": f"scale-{scale.code}",
                "source_title": scale.title,
                "topic": "ШКАЛЫ",
                "text": (
                    f"Шкала {scale.code} — {scale.title}. Вопросы: {items}. "
                    f"Пороги тяжести: {thresholds}. "
                    f"Суицидальный пункт: {scale.suicidal_item or 'нет'}."
                ),
            }
        )
    return docs


def _triage_docs() -> list[dict]:
    docs = [
        {
            "guideline_id": "triage-rule",
            "source_title": "Правила триажа Clinical Compass",
            "topic": "ТРИАЖ",
            "text": (
                "Триаж результата теста: если есть флаг суицидальной идеации ИЛИ "
                "тяжесть SEVERE — результат RED. Если тяжесть MILD или MODERATE — "
                "YELLOW. Если тяжесть NONE без флагов — GREEN."
            ),
        },
        {
            "guideline_id": "triage-red-eval",
            "source_title": "Назначение триажа RED",
            "topic": "ТРИАЖ",
            "text": (
                "RED означает тяжёлое состояние или суицидальный риск. Пациенту "
                "показываются контакты помощи (8-800-2000-122, 112) и создаётся "
                "эскалация для врача в журнале."
            ),
        },
    ]
    return docs


def _redlines_docs() -> list[dict]:
    return [
        {
            "guideline_id": "redline-diagnosis",
            "source_title": "Красная линия: постановка диагноза",
            "topic": "РЕКОМЕНДАЦИИ",
            "text": (
                "Постановка диагноза — функция врача (323-ФЗ). Инструмент проводит "
                "скрининг по валидированным шкалам (PHQ-9, GAD-7, HADS) и выдаёт "
                "триаж, но не ставит диагноз и не назначает терапию."
            ),
        },
        {
            "guideline_id": "redline-therapy",
            "source_title": "Красная линия: терапия",
            "topic": "РЕКОМЕНДАЦИИ",
            "text": (
                "Назначение, изменение и отмена терапии — только врачом (323-ФЗ "
                "ст.36.2, приказ Минздрава №965н; врач-врач телемедицинские "
                "консультации и консилиумы допустимы без очного приёма). "
                "Инструмент — вспомогательный: справка по инструкции/показаниям "
                "препарата, мониторинг динамики, эскалация при ухудшении."
            ),
        },
    ]

_GUIDELINES_FILE = Path(__file__).parent / "data" / "guidelines.json"
_GUIDELINES_CACHE = Path(__file__).parent / "data" / "guidelines_cache"


def _guideline_docs(with_full_text: bool = False) -> list[dict]:
    """Чанки клинических рекомендаций Минздрава (критерий кейса 1 — указать источник).

    Метаданный чанк (guideline_id, код, разработчик, URL, краткая справка) строится
    всегда — он цитируем в RAG-ответах без сети. Полный текст КР защищён авторским
    правом (docs/08 §2): индексируется из кеша data/guidelines_cache/<id>.txt только
    при with_full_text=True и наличии файла.
    """
    if not _GUIDELINES_FILE.exists():
        return []
    data = json.loads(_GUIDELINES_FILE.read_text(encoding="utf-8"))
    docs: list[dict] = []
    for g in data.get("guidelines", []):
        code = g["code"]
        docs.append(
            {
                "guideline_id": f"kr-{g['id']}",
                "source_title": g["title"],
                "topic": "РЕКОМЕНДАЦИИ",
                "text": (
                    f"{g['title']}. Код рубрикатора КР Минздрава: {code}, ред. {g['revision']}. "
                    f"Разработчик: {g['developer']}. {g['summary']} "
                    f"Источник: рубрикатор КР Минздрава {g['rubricator_url']}."
                ),
            }
        )
        if with_full_text:
            cache = _GUIDELINES_CACHE / f"{g['id']}.txt"
            if cache.exists():
                full = cache.read_text(encoding="utf-8")
                for i in range(0, len(full), 1800):
                    chunk = full[i : i + 1800].strip()
                    if chunk:
                        docs.append(
                            {
                                "guideline_id": f"kr-{g['id']}-t{i // 1800}",
                                "source_title": g["title"],
                                "topic": "РЕКОМЕНДАЦИИ",
                                "text": chunk,
                            }
                        )
    return docs


def _drug_docs(rows: list | None = None) -> list[dict]:
    """Чанки по препаратам из таблицы drugs (источник — ГРЛС, усл.7 кейса).

    Таблица — единственный источник правды: seed.py грузит api/app/data/drugs.json
    в drugs, отсюда же строятся чанки для Qdrant. На препарат — основной чанк
    (показания/дозировка/механизм) + отдельный чанк НЯ/противопоказаний, чтобы
    retrieval по «побочные эффекты <препарат>» попадал точно. Тема ЛЕКАРСТВА.

    В request-path строки передаёт `load_drug_docs` (async-сессия запроса);
    sync-фолбэк ниже — только CLI/тесты (build_corpus без аргумента).
    """
    if rows is None:
        from app.db import SyncSessionLocal
        from app.models import Drug

        with SyncSessionLocal() as s:
            rows = list(s.execute(select(Drug).order_by(Drug.drug_id)).scalars().all())

    docs: list[dict] = []
    for d in rows:
        title = f"{d.trade_name} ({d.inn})"
        ru = f"РУ {d.reg_certificate} от {d.reg_date}"
        docs.append(
            {
                "guideline_id": f"drug-{d.drug_id}",
                "source_title": f"{title} — инструкция",
                "topic": "ЛЕКАРСТВА",
                "text": (
                    f"Препарат {title}. Лекарственная форма: {d.form}. "
                    f"{ru}. Механизм действия: {d.pharmacology} "
                    f"Показания к применению: {d.indications} "
                    f"Режим дозирования: {d.dosage} "
                    f"{d.special_notes}"
                ),
            }
        )
        docs.append(
            {
                "guideline_id": f"drug-{d.drug_id}-safety",
                "source_title": f"{title} — нежелательные явления и противопоказания",
                "topic": "ЛЕКАРСТВА",
                "text": (
                    f"Препарат {title}. Противопоказания: {d.contraindications} "
                    f"Нежелательные явления: {d.adverse_events} "
                    f"{d.special_notes}"
                ),
            }
        )
    return docs


# Источники научных статей (отбор — человеком по критериям docs/13, свежесть ≤5 лет).
# Метаданные живут только здесь и в docs/13: публичный контракт цитат их не несёт
# (5ffa299), а фильтрация свежести — прерогатива куратора при засеве.
#   article-vortioxetine-bdr-gtr      — «РКИ, 8 недель», 2025, https://researchportal.bath.ac.uk
#   article-trd-clinimetric           — «Обзор», 2025, https://cris.unibo.it/handle/11585/1042813
#   article-trd-realworld             — «Когортное исследование», 2025, https://researchportal.bath.ac.uk
#   article-complex-approach          — «Обзор», 2025, https://researchportal.bath.ac.uk
#   article-adr-pharmacovigilance-rf  — «Анализ базы НР (Росздравнадзор)», 2025, https://risksafety.ru/jour/article/view/521
def _article_docs() -> list[dict]:
    """Чанки научных статей, прошедших критерии docs/13 (отбор — человеком).

    topic=НАУЧНЫЕ-СТАТЬИ; провенанс источников — в комментарии выше.
    """
    return [
        {
            "guideline_id": "article-vortioxetine-bdr-gtr",
            "source_title": "Вортиоксетин при коморбидном БДР+ГТР — 8 недель (РКИ)",
            "topic": "НАУЧНЫЕ-СТАТЬИ",
            "text": (
                "Эффективность вортиоксетина при коморбидном большом депрессивном "
                "расстройстве и генерализованном тревожном расстройстве: РКИ, 8 "
                "недель. Источник: 2025, критерии отбора docs/13. Статья — пример "
                "валидного входа в КБ; не заменяет клинические рекомендации Минздрава."
            ),
        },
        {
            "guideline_id": "article-trd-clinimetric",
            "source_title": "ТРД: клинометрический подход (истинная/псевдорезистентность)",
            "topic": "НАУЧНЫЕ-СТАТЬИ",
            "text": (
                "Дифференциация истинной и псевдорезистентной депрессии и "
                "остаточных симптомов: выбор психотерапии (КПТ, MBCT, "
                "интерперсональная) вместо усиления фармакотерапии. Источник: "
                "2025, критерии отбора docs/13."
            ),
        },
        {
            "guideline_id": "article-trd-realworld",
            "source_title": "ТРД в реальной практике: рецидивы, коморбидности (когорта)",
            "topic": "НАУЧНЫЕ-СТАТЬИ",
            "text": (
                "Крупное реальное исследование траекторий лечения резистентной "
                "депрессии: повышенный риск рецидивов, коморбидной тревоги и "
                "расстройств личности, самоповреждений, сердечно-сосудистых "
                "заболеваний. Важно для мониторинга динамики пациента. Источник: "
                "2025, критерии отбора docs/13."
            ),
        },
        {
            "guideline_id": "article-complex-approach",
            "source_title": "Комплексный подход при тревоге+депрессии: пределы монотерапии",
            "topic": "НАУЧНЫЕ-СТАТЬИ",
            "text": (
                "Пределы медикаментозной стратегии при коморбидной тревоге и "
                "депрессии: аргумент за комплексный подход (фармако + "
                "психотерапия). Справочный материал для врача в библиотеке. "
                "Источник: 2025, критерии отбора docs/13."
            ),
        },
        {
            "guideline_id": "article-adr-pharmacovigilance-rf",
            "source_title": "Фармаконадзор РФ 2019–2024: профиль НР антидепрессантов",
            "topic": "НАУЧНЫЕ-СТАТЬИ",
            "text": (
                "Профиль безопасности антидепрессантов по данным отечественного "
                "фармаконадзора: амитриптилин — наибольшее число сообщений о "
                "нежелательных реакциях (n=470); у вортиоксетина статистически "
                "значимые НР (психика, сердечно-сосудистая, эндокринная системы). "
                "Отечественные данные для раздела НЯ библиотеки препаратов. "
                "Источник: Султанова, 2025, критерии отбора docs/13."
            ),
        },
    ]


async def load_drug_docs(db) -> list[dict]:
    """drug-чанки через async-сессию запроса (без sync Session в request-path)."""
    from app.models import Drug

    rows = (await db.scalars(select(Drug).order_by(Drug.drug_id))).all()
    return _drug_docs(list(rows))


def _head_chunks() -> list[dict]:
    """Статические секции корпуса, не зависящие от with_guidelines."""
    return _scale_docs() + _triage_docs() + _redlines_docs()


def _tail_chunks(with_guidelines: bool) -> list[dict]:
    return _guideline_docs(with_full_text=with_guidelines) + _article_docs()


# Кеш собранного корпуса. build_corpus() вызывается на запрос агента (лексический
# fallback в search_kb) и без кеша каждый раз ходит в БД за препаратами — держим
# готовый корпус в процессе. Записи неизменяемы (MappingProxyType), чтобы
# вызывающий не мог испортить кеш. Инвалидация: TTL (CC_KB_CORPUS_TTL_SECONDS) и
# явная clear_corpus_cache() из писателей справочника (seed CLI, тестовые
# фикстуры); runtime-писателей drugs/КР в API нет, поэтому TTL — единственная
# инвалидация на запросном пути.
#
# Ключи: ("corpus", with_guidelines) — полный снимок для CLI/лексического
# fallback (препараты в нём читаются из БД через sync-сессию); ("head",) и
# ("tail", with_guidelines) — статика без препаратов. Request-path передаёт
# препараты из сессии запроса (build_corpus(drug_docs=...)): такой список НЕ
# кешируется — он принадлежит запросу.
_corpus_lock = threading.Lock()
_corpus_cache: dict[tuple, tuple[float, tuple[Mapping[str, Any], ...]]] = {}
_corpus_generation = 0  # растёт при каждой инвалидации: сборка «в полёте» не должна кешироваться


def clear_corpus_cache() -> None:
    """Сбросить кеш корпуса (после записи в drugs/КР, в CLI-сиде и тестах)."""
    global _corpus_generation
    with _corpus_lock:
        _corpus_cache.clear()
        _corpus_generation += 1


def _cached(key: tuple, build) -> tuple[Mapping[str, Any], ...]:
    """Снимок по ключу с TTL. Сборка — ВНЕ лока.

    Под локом был бы «один промах на всех», но сборка полного корпуса читает
    drugs из БД sync-сессией: если БД притормозила, лок держал бы все потоки
    keyword-fallback. Поэтому допускаем редкие дубли сборки на холодном старте,
    а под локом только читаем/пишем кеш. Исключения не кешируются — при ошибке
    следующий вызов попытается собрать снимок снова.
    """
    ttl = settings.kb_corpus_ttl_seconds
    with _corpus_lock:
        generation = _corpus_generation
        cached = _corpus_cache.get(key) if ttl > 0 else None
        if cached is not None and time.monotonic() - cached[0] < ttl:
            return cached[1]
    snapshot = tuple(MappingProxyType(dict(d)) for d in build())
    with _corpus_lock:
        # Пока собирали, могла пройти clear_corpus_cache() (seed/тест/будущий
        # писатель): тогда наш снимок уже устарел и класть его в кеш нельзя.
        if generation != _corpus_generation:
            return snapshot
        if ttl > 0:
            cached = _corpus_cache.get(key)
            if cached is not None and time.monotonic() - cached[0] < ttl:
                return cached[1]
            _corpus_cache[key] = (time.monotonic(), snapshot)
        else:
            _corpus_cache.pop(key, None)
    return snapshot


def _immutable(docs: list[dict]) -> list[Mapping[str, Any]]:
    return [MappingProxyType(dict(d)) for d in docs]


def _assemble(with_guidelines: bool, drugs: list[dict]) -> list[dict]:
    return (
        _head_chunks() + list(drugs) + _tail_chunks(with_guidelines)
    )


def build_corpus(
    with_guidelines: bool = False, drug_docs: list[dict] | None = None
) -> tuple[Mapping[str, Any], ...]:
    """Чанки KB: шкалы, триаж, красные линии, препараты, КР, статьи (docs/08 §5).

    Без drug_docs возвращается закешированный на CC_KB_CORPUS_TTL_SECONDS
    снимок (<= 0 — кеш выключен), препараты в него читаются из БД.
    С drug_docs (request-path, см. load_drug_docs) кешируется только
    статическая часть, а переданный список оборачивается в неизменяемые
    копии — он принадлежит запросу и не должен попадать в кеш.
    """
    if drug_docs is None:
        return _cached(
            ("corpus", with_guidelines),
            lambda: _assemble(with_guidelines, _drug_docs()),
        )
    head = _cached(("head",), _head_chunks)
    tail = _cached(("tail", with_guidelines), lambda: _tail_chunks(with_guidelines))
    return head + tuple(_immutable(drug_docs)) + tail


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=settings.qdrant_url)
    p.add_argument("--reset", action="store_true", help="drop и пересоздать коллекцию")
    p.add_argument("--with-guidelines", action="store_true",
                   help="индексировать полный текст КР из data/guidelines_cache (метаданные всегда)")
    args = p.parse_args()

    client = QdrantClient(url=args.url, timeout=10)
    # depends_on healthy не гарантирует DNS/готовность в момент старта — ждём.
    last_err: Exception | None = None
    for attempt in range(30):
        try:
            client.get_collections()
            break
        except Exception as exc:  # noqa: BLE001 — ждём готовности внешнего сервиса
            last_err = exc
            time.sleep(1)
    else:
        raise SystemExit(f"Qdrant недоступен по {args.url}: {last_err}")

    if args.reset and client.collection_exists(kb.COLLECTION):
        client.delete_collection(kb.COLLECTION)

    # build_corpus()._drug_docs читает препараты из таблицы drugs — нужен engine.
    from app import db as _db
    if _db.engine is None:
        _db.setup(settings.database_url)

    # Переиндексация всегда читает БД/файлы заново: не индексируем закешированный корпус.
    clear_corpus_cache()
    docs = build_corpus(with_guidelines=args.with_guidelines)
    n = kb.upsert_documents(client, build_embedder(), docs)
    total = kb.count_points(client)
    print(f"Индексировано {n} чанков; всего в коллекции {kb.COLLECTION}: {total}")


if __name__ == "__main__":
    main()
