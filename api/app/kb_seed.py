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

_DRUGS_FILE = Path(__file__).parent / "data" / "drugs.json"


def _drug_docs() -> list[dict]:
    """Чанки по препаратам из api/app/data/drugs.json (источник — ГРЛС, усл.7 кейса).

    На препарат — основной чанк (показания/дозировка/механизм) + отдельный чанк
    нежелательных явлений и противопоказаний, чтобы retrieval по «побочные эффекты
    <препарат>» попадал точно. Тема ЛЕКАРСТВА.
    """
    if not _DRUGS_FILE.exists():
        return []
    data = json.loads(_DRUGS_FILE.read_text(encoding="utf-8"))
    docs: list[dict] = []
    for d in data.get("drugs", []):
        title = f"{d['trade_name']} ({d['inn']})"
        ru = f"РУ {d['reg_certificate']} от {d['reg_date']}"
        docs.append(
            {
                "guideline_id": f"drug-{d['drug_id']}",
                "source_title": f"{title} — инструкция",
                "topic": "ЛЕКАРСТВА",
                "text": (
                    f"Препарат {title}. Лекарственная форма: {d['form']}. "
                    f"{ru}. Механизм действия: {d['pharmacology']} "
                    f"Показания к применению: {d['indications']} "
                    f"Режим дозирования: {d['dosage']} "
                    f"{d['special_notes']}"
                ),
            }
        )
        docs.append(
            {
                "guideline_id": f"drug-{d['drug_id']}-safety",
                "source_title": f"{title} — нежелательные явления и противопоказания",
                "topic": "ЛЕКАРСТВА",
                "text": (
                    f"Препарат {title}. Противопоказания: {d['contraindications']} "
                    f"Нежелательные явления: {d['adverse_events']} "
                    f"{d['special_notes']}"
                ),
            }
        )
    return docs


def build_corpus() -> list[dict]:
    return _scale_docs() + _triage_docs() + _redlines_docs() + _drug_docs()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=settings.qdrant_url)
    p.add_argument("--reset", action="store_true", help="drop и пересоздать коллекцию")
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

    docs = build_corpus()
    n = kb.upsert_documents(client, build_embedder(), docs)
    total = kb.count_points(client)
    print(f"Индексировано {n} чанков; всего в коллекции {kb.COLLECTION}: {total}")


if __name__ == "__main__":
    main()
