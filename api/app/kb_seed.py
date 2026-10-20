"""Сбор базы знаний ИИ-агента в Qdrant (docs/08 §5). Офлайн-демо.

CLI: python -m app.kb_seed [--url CC_QDRANT_URL] [--reset]

Корпус строится из СОБСТВЕННОГО контента движка (docs/08 §2): описания шкал,
правила триажа, красные линии. Только открытый не-ПДн материал (docs/07 §3.5).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

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


def _drug_docs() -> list[dict]:
    """Чанки по препаратам из таблицы drugs (источник — ГРЛС, усл.7 кейса).

    Таблица — единственный источник правды: seed.py грузит api/app/data/drugs.json
    в drugs, отсюда же строятся чанки для Qdrant. На препарат — основной чанк
    (показания/дозировка/механизм) + отдельный чанк НЯ/противопоказаний, чтобы
    retrieval по «побочные эффекты <препарат>» попадал точно. Тема ЛЕКАРСТВА.
    """
    from app.db import SessionLocal
    from app.models import Drug

    docs: list[dict] = []
    with SessionLocal() as s:
        rows = s.execute(
            select(Drug).order_by(Drug.drug_id)
        ).scalars().all()
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


def build_corpus(with_guidelines: bool = False) -> list[dict]:
    return (
        _scale_docs() + _triage_docs() + _redlines_docs()
        + _drug_docs() + _guideline_docs(with_full_text=with_guidelines)
        + _article_docs()
    )


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

    docs = build_corpus(with_guidelines=args.with_guidelines)
    n = kb.upsert_documents(client, build_embedder(), docs)
    total = kb.count_points(client)
    print(f"Индексировано {n} чанков; всего в коллекции {kb.COLLECTION}: {total}")


if __name__ == "__main__":
    main()
