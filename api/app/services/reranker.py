"""Кросс-энкодерный реранк выдачи базы знаний (docs/08 §4).

Зачем: dense/sparse-поиск оптимизирован на recall@пула (top-50), а порядок
внутри пула для клинического ответа решает кросс-энкодер — он видит пару
(запрос, чанк) целиком и заметно поднимает nDCG@3 на RU→EN перефразах
(bench/REPORT-retrieval.md).

Конфигурация: CC_RERANKER=off | <hf-model-id>. Зависимости опциональны
(extra `rerank`: sentence-transformers + torch). Любая проблема — модель не
скачана, пакета нет, CUDA недоступна — НЕ роняет запрос: пишем warning и
возвращаем порядок RRF без изменений (graceful degradation, как у эмбеддеров).

Инвариант: реранк не может вернуть чанк, исключённый врачом (exclude_ids).
Qdrant уже фильтрует такие точки, но реранк применяет фильтр повторно —
на случай смены реализации поиска (defense in depth).
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from app.config import settings

log = logging.getLogger(__name__)

# Сколько чанков отдавать кросс-энкодеру за раз (память GPU × длина пары).
_BATCH = 8


class Reranker(Protocol):
    model_id: str

    def rerank(
        self,
        query: str,
        hits: list[dict],
        limit: int,
        *,
        exclude_ids: set[str] | None = None,
    ) -> list[dict]:
        """Отсортировать hit-ы по паре (query, text) и вернуть первые limit."""
        ...


class CrossEncoderReranker:
    """sentence-transformers CrossEncoder (bge-reranker-v2-m3, jina, DiTy, Qwen3)."""

    def __init__(
        self,
        model_id: str,
        *,
        device: str = "auto",
        max_length: int = 512,
    ) -> None:
        from sentence_transformers import CrossEncoder  # опциональная зависимость

        self.model_id = model_id
        resolved = None if (device or "auto") == "auto" else device
        self._m = CrossEncoder(model_id, device=resolved, max_length=max_length)
        self._batch = _BATCH

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        out = self._m.predict(
            [(query, t) for t in texts],
            batch_size=self._batch,
            show_progress_bar=False,
        )
        return [float(x) for x in out]

    def rerank(
        self,
        query: str,
        hits: list[dict],
        limit: int,
        *,
        exclude_ids: set[str] | None = None,
    ) -> list[dict]:
        if exclude_ids:
            hits = [h for h in hits if h.get("guideline_id") not in exclude_ids]
        if not hits:
            return []
        scores = self.score(query, [h.get("text", "") for h in hits])
        # stable sort: равные скоры сохраняют порядок RRF (детерминизм выдачи)
        order = sorted(range(len(hits)), key=lambda i: (-scores[i], i))
        return [
            {
                **hits[i],
                # score — скор кросс-энкодера (порядок выдачи), retrieval_score —
                # скор первичного поиска: по нему работает порог профиля, шкала
                # logit-ов реранкера с косинусом несравнима.
                "score": scores[i],
                "rerank_score": scores[i],
                "retrieval_score": hits[i].get("score", 0.0),
            }
            for i in order[:limit]
        ]


_reranker: Reranker | None = None
_reranker_loaded = False
_lock = threading.Lock()


def build_reranker(spec: str | None = None) -> Reranker | None:
    """Фабрика по CC_RERANKER. 'off'/пусто → None; ошибка → None + warning."""
    chosen = (spec if spec is not None else settings.reranker) or "off"
    chosen = chosen.strip()
    if chosen.lower() in {"off", "none", "no", ""}:
        return None
    try:
        return CrossEncoderReranker(chosen, device=settings.embedder_device)
    except ImportError as exc:
        log.warning(
            "CC_RERANKER=%s: нет опциональных зависимостей (%s). Установите extra "
            "`rerank` (`uv sync --extra rerank`) — до этого порядок выдачи остаётся RRF.",
            chosen,
            exc,
        )
    except Exception as exc:  # noqa: BLE001 — сеть/HF-кэш/веса: не роняем запрос
        log.warning(
            "CC_RERANKER=%s недоступен (%s) — порядок выдачи остаётся RRF.",
            chosen,
            exc,
        )
    return None


def get_reranker() -> Reranker | None:
    """Процессный синглтон реранкера (модель грузится один раз, лениво)."""
    global _reranker, _reranker_loaded
    if not _reranker_loaded:
        with _lock:
            if not _reranker_loaded:
                _reranker = build_reranker()
                _reranker_loaded = True
    return _reranker


def reset_reranker() -> None:
    """Сбросить синглтон (тесты/смена CC_RERANKER без перезапуска процесса)."""
    global _reranker, _reranker_loaded
    with _lock:
        _reranker = None
        _reranker_loaded = False
