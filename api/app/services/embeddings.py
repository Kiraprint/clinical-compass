"""СПИ эмбеддингов для базы знаний (docs/08 §4).

По умолчанию — детерминированный офлайн HashEmbedder (работает без сети и
модели, пригоден для демо и тестов). Продуктивный BgeM3Embedder — за тем же
интерфейсом, подключается при наличии зависимостей (docs/03 §1.3, SPI).
Не-ПДн: корпус — открытый клинический контент, без персональных данных.
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

DENSE_SIZE = 512          # размер dense-вектора (bge-m3 = 1024; демо 512)
SPARSE_BUCKETS = 65536    # пространство хеш-индексов sparse


class Embedder(Protocol):
    dense_size: int

    def dense(self, text: str) -> list[float]:
        ...

    def sparse(self, text: str) -> tuple[list[int], list[float]]:
        """(indices, values) — лексические индексы и веса."""
        ...


def _tokens(text: str) -> list[str]:
    # Токенизация для русского/латиницы; сохраняем слова и короткие n-граммы терминов.
    lower = text.lower()
    words = re.findall(r"[a-zа-яё0-9][a-zа-яё0-9\-]{1,39}", lower)
    tokens = list(words)
    # 3-граммы из слов — чтобы ловить смысловое сходство даже при разных падежах.
    for i in range(len(words) - 2):
        tokens.append(" ".join(words[i : i + 3]))
    return tokens


def _hash_index(token: str, buckets: int) -> int:
    return int(hashlib.md5(token.encode("utf-8")).hexdigest()[:8], 16) % buckets


class HashEmbedder:
    """Feature-hashing dense + лексический sparse. Детерминирован, офлайн."""

    dense_size = DENSE_SIZE

    def dense(self, text: str) -> list[float]:
        tokens = _tokens(text)
        vec = [0.0] * self.dense_size
        for tok in tokens:
            idx = _hash_index(tok, self.dense_size)
            vec[idx] += 1.0
        # L2-нормировка для косинусной близости.
        norm = sum(v * v for v in vec) ** 0.5
        if norm:
            vec = [v / norm for v in vec]
        return vec

    def sparse(self, text: str) -> tuple[list[int], list[float]]:
        weights: dict[int, float] = {}
        for tok in _tokens(text):
            idx = _hash_index(tok, SPARSE_BUCKETS)
            weights[idx] = weights.get(idx, 0.0) + 1.0
        indices = sorted(weights)
        values = [weights[i] for i in indices]
        return indices, values


# Продуктивный эмбеддер — за тем же интерфейсом. Демо/тесты не требуют модели.
# Выбирается через settings.embedder (CC_EMBEDDER) в build_embedder().
class BgeM3Embedder:
    """bge-m3 (dense 1024 + sparse + colbert). MIT, 100+ языков, self-host (docs/07).

    Требует тяжёлой модели (FlagEmbedding) — импортируется лениво, чтобы тесты и
    демо без модели не тащили зависимость. Sparse — подлинные лексические веса
    модели (лучше лексического хеша; docs/08 §4).
    """

    dense_size = 1024

    def __init__(self) -> None:
        from FlagEmbedding import BGEM3FlagModel  # лениво

        self._m = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)

    def dense(self, text: str) -> list[float]:
        out = self._m.encode([text], return_dense=True, return_sparse=True, max_length=2048)
        return out["dense_vecs"][0].tolist()

    def sparse(self, text: str) -> tuple[list[int], list[float]]:
        out = self._m.encode([text], return_sparse=True, max_length=2048)
        w = out["lexical_weights"][0]
        return list(w.keys()), list(w.values())


def build_embedder() -> Embedder:
    """Фабрика по CC_EMBEDDER: hash | bge-m3. По умолчанию hash (демо/тесты)."""
    from app.config import settings

    kind = getattr(settings, "embedder", "hash")
    if kind == "bge-m3":
        return BgeM3Embedder()
    return HashEmbedder()


__all__ = [
    "Embedder",
    "HashEmbedder",
    "BgeM3Embedder",
    "build_embedder",
    "DENSE_SIZE",
    "SPARSE_BUCKETS",
]
