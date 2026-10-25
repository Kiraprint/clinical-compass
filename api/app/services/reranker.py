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
import re
import threading
from typing import Protocol

from app.config import settings

log = logging.getLogger(__name__)

# Сколько чанков отдавать кросс-энкодеру за раз (память GPU × длина пары).
_BATCH = 8


def _query_prefixes(query: str) -> set[str]:
    """Стем-префиксы слов запроса (5 символов) — грубая замена стеммеру.

    Нужны только для выбора окна чанка: «сертралина» и «сертралин» дают общий
    префикс «сертр», поэтому окно находится без словаря/морфологии.
    """
    words = [w for w in re.findall(r"[\w-]{4,}", (query or "").lower()) if len(w) >= 4]
    return {w[:5] for w in words}


def best_window(text: str, query: str, width: int) -> str:
    """Окно чанка, лучше всего совпадающее с запросом (CC_RERANK_WINDOW).

    Кросс-энкодер с max_length 512 видит ~512 токенов: у чанка 1800 знаков хвост
    с ответом отбрасывается. Скользим окном width с шагом width/2 и берём
    максимум по числу префиксов слов запроса внутри окна (тай-брейк — раньше).
    """
    if width <= 0 or len(text) <= width:
        return text
    prefixes = _query_prefixes(query)
    if not prefixes:
        return text[:width]
    step = max(width // 2, 1)
    best, best_hits = text[:width], -1
    for start in range(0, len(text) - width + 1, step):
        frag = text[start : start + width].lower()
        hits = sum(1 for p in prefixes if p in frag)
        if hits > best_hits:
            best, best_hits = text[start : start + width], hits
    return best


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

    # Ширина окна по умолчанию (0 — начало чанка); __init__ берёт CC_RERANK_WINDOW.
    # Атрибут класса — чтобы подставные объекты (тесты) не падали на score().
    _window = 0

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
        # CC_RERANK_WINDOW: 0 — CE видит начало чанка; >0 — лучшее окно такой
        # ширины (docs/08 §4.8). Длинные чанки без окна теряют ответ в хвосте.
        self._window = max(int(settings.rerank_window or 0), 0)

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        if self._window:
            texts = [best_window(t, query, self._window) for t in texts]
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

class RemoteReranker:
    """Реранк в ML-сайдкаре на GPU (docs/08 §4.7): POST /v1/rerank.

    Один ретрай на транспортной ошибке; если сайдкар так и не ответил,
    `MlUnavailable` уходит наверх, а `kb.hybrid_search` оставляет порядок RRF:
    прод обязан отвечать, реранк — улучшение, а не условие работы.
    """

    model_id = "remote"

    # Ширина окна чанка (0 — начало чанка); __init__ берёт CC_RERANK_WINDOW.
    _window = 0

    def __init__(self) -> None:
        from app.services import ml_client

        if not ml_client.enabled():
            raise RuntimeError("CC_RERANKER=remote требует CC_ML_URL (base-url ML-сайдкара)")
        # CC_RERANK_WINDOW действует одинаково на обоих путях реранка: сайдкар
        # с тем же max_length 512 теряет хвост длинного чанка, поэтому окно
        # режется ДО отправки (docs/08 §4.8).
        self._window = max(int(settings.rerank_window or 0), 0)

    def score(self, query: str, texts: list[str]) -> list[float]:
        # Повтор на сетевой сбой/5xx делает сам клиент (ml_client.rerank):
        # второй цикл ретраев здесь только удвоил бы таймаут запроса.
        if not texts:
            return []
        from app.services import ml_client

        if self._window:
            texts = [best_window(t, query, self._window) for t in texts]
        return ml_client.rerank(query, texts)

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
        if chosen.lower() == "remote":
            return RemoteReranker()
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
