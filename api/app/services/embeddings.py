"""СПИ эмбеддингов для базы знаний (docs/08 §4).

По умолчанию — детерминированный офлайн HashEmbedder (работает без сети и
модели, пригоден для демо и тестов). Продуктивный BgeM3Embedder — за тем же
интерфейсом, подключается при наличии зависимостей (docs/03 §1.3, SPI).
Не-ПДн: корпус — открытый клинический контент, без персональных данных.

Профили (CC_EMBEDDER) и их развёртывание — docs/08 §4; качество RU-поиска и
воспроизводимый замер — bench/eval_app_path.py.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

DENSE_SIZE = 512          # размер dense-вектора (bge-m3 = 1024; демо 512)
SPARSE_BUCKETS = 65536    # пространство хеш-индексов sparse

# Сколько текстов держать в кэше энкодера (upsert считает dense и sparse одного
# и того же текста по отдельности; кэш убирает второй прогон модели).
_CACHE_SIZE = 256


class EmbedderConfigError(RuntimeError):
    """Эмбеддер невозможно сконфигурировать: нет зависимостей/модели/устройства.

    Наследует RuntimeError, чтобы старт приложения падал сразу с понятным
    сообщением, а не в середине запроса (docs/08 §4).
    """


@dataclass(frozen=True)
class EmbedderProfile:
    """Поддерживаемый профиль CC_EMBEDDER.

    default_min_score — порог «лучший чанк нерелевантен» под калибровку ИМЕННО
    этой модели (docs/08 §4): шкала косинуса у профилей разная, поэтому
    CC_KB_MIN_SCORE по умолчанию берётся отсюда, а не единым числом.
    query_prefix/passage_prefix — инструкции моделей, требующих их (e5, Qwen3).
    """

    kind: str
    model_id: str
    dense_size: int
    use_sparse: bool = False
    default_min_score: float = 0.45
    query_prefix: str = ""
    passage_prefix: str = ""


# Профили — единственный источник истины для размерности dense: коллекция
# Qdrant создаётся под неё (docs/08 §3), при смене нужна переиндексация.
# default_min_score перемерян на bench/data/kb_queries.jsonl (bench/REPORT-retrieval.md).
_INSTRUCT_QWEN = (
    "Instruct: Given a clinical question, retrieve passages that answer the question\nQuery: "
)
PROFILES: dict[str, EmbedderProfile] = {
    "hash": EmbedderProfile("hash", "offline/hash", DENSE_SIZE, use_sparse=True, default_min_score=0.45),
    "bge-m3": EmbedderProfile("bge-m3", "BAAI/bge-m3", 1024, default_min_score=0.45),
    "qwen3-0.6b": EmbedderProfile(
        "st", "Qwen/Qwen3-Embedding-0.6B", 1024,
        default_min_score=0.40, query_prefix=_INSTRUCT_QWEN,
    ),
    "e5-large-instruct": EmbedderProfile(
        "st", "intfloat/multilingual-e5-large-instruct", 1024,
        default_min_score=0.80,
        query_prefix="Instruct: Given a clinical question, retrieve passages that answer the question\nQuery: ",
    ),
    "user-bge-m3": EmbedderProfile("st", "deepvk/USER-bge-m3", 1024, default_min_score=0.55),
    # Профили ML-сайдкара (GPU): model_id — id профиля в сайдкаре, dense_size —
    # ожидаемая размерность (проверяется при старте, docs/08 §4.7).
    "remote-bge-m3": EmbedderProfile("remote", "bge-m3", 1024, default_min_score=0.45),
    "remote-qwen3-0.6b": EmbedderProfile("remote", "qwen3-0.6b", 1024, default_min_score=0.40),
    "remote-user-bge-m3": EmbedderProfile("remote", "user-bge-m3", 1024, default_min_score=0.55),
    "remote-e5-large-instruct": EmbedderProfile("remote", "e5-large-instruct", 1024, default_min_score=0.80),
}


class Embedder(Protocol):
    dense_size: int
    # Стратегия поиска (kb.hybrid_search): True — dense+sparse RRF, False — dense-only.
    use_sparse: bool

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
    use_sparse = True  # лексические веса точны: поиск dense+sparse → RRF

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

    Требует тяжёлой модели (FlagEmbedding + torch) — импортируется лениво, чтобы
    тесты и демо без модели не тащили зависимость (extra `embeddings`, docs/08 §4).
    Sparse-веса модели считаются при индексации, но в выдаче не участвуют:
    прод-профиль ищет dense-only (kb.hybrid_search, bench/eval_app_path.py).
    """
    use_sparse = False  # dense-only: RRF с лексикой режет RU-качество (см. hybrid_search)

    def __init__(
        self,
        model_id: str = "BAAI/bge-m3",
        *,
        device: str = "auto",
        batch_size: int = 8,
        max_length: int = 2048,
    ) -> None:
        if batch_size < 1:
            raise EmbedderConfigError(
                f"CC_EMBEDDER_BATCH_SIZE={batch_size}: ожидается целое >= 1."
            )
        if max_length < 16:
            raise EmbedderConfigError(
                f"CC_EMBEDDER_MAX_LENGTH={max_length}: ожидается >= 16."
            )
        resolved_device = _resolve_device(device)
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:  # нет ни FlagEmbedding, ни torch
            raise EmbedderConfigError(
                "CC_EMBEDDER=bge-m3 требует опциональных зависимостей FlagEmbedding+torch. "
                "Установите extra `embeddings` (uv: `uv sync --extra embeddings`, "
                "Docker: `uv sync --frozen --extra embeddings`), либо верните CC_EMBEDDER=hash. "
                "Базовый образ API намеренно без torch (docs/08 §4)."
            ) from exc
        self.model_id = model_id
        self.device = resolved_device
        self._batch_size = batch_size
        self._max_length = max_length
        try:
            self._m = BGEM3FlagModel(
                model_id,
                use_fp16=self.device.startswith("cuda"),
                device=self.device,
            )
        except Exception as exc:  # сеть/кэш HF/несовместимые веса
            raise EmbedderConfigError(
                f"Не удалось загрузить эмбеддер '{model_id}' на устройстве "
                f"'{self.device}': {exc}. Проверьте кэш HF и сеть (модель качается "
                f"при первом запуске) или укажите CC_EMBEDDER_DEVICE=cpu."
            ) from exc
        self.dense_size = _dense_size(self._m)
        self._cache: dict[str, tuple[list[float], list[int], list[float]]] = {}

    def _encode(self, text: str) -> tuple[list[float], list[int], list[float]]:
        hit = self._cache.get(text)
        if hit is not None:
            return hit
        out = self._m.encode(
            [text],
            return_dense=True,
            return_sparse=True,
            batch_size=self._batch_size,
            max_length=self._max_length,
        )
        weights = out["lexical_weights"][0]
        encoded = (
            out["dense_vecs"][0].tolist(),
            list(weights.keys()),
            list(weights.values()),
        )
        if len(self._cache) >= _CACHE_SIZE:  # FIFO: держим память ограниченной
            self._cache.pop(next(iter(self._cache)))
        self._cache[text] = encoded
        return encoded

    def dense(self, text: str) -> list[float]:
        return self._encode(text)[0]

    def sparse(self, text: str) -> tuple[list[int], list[float]]:
        _, indices, values = self._encode(text)
        return indices, values



class SentenceTransformerEmbedder:
    """Dense-эмбеддер через sentence-transformers под общим SPI (docs/08 §4).

    Профили qwen3-0.6b / e5-large-instruct / user-bge-m3. Префиксы запроса и
    пассажа — из EmbedderProfile (инструкции моделей). Sparse-канал лексический
    (hash): коллекция всегда содержит sparse-вектор, но профиль по умолчанию
    ищет dense-only (use_sparse=False).
    """

    use_sparse = False  # стратегия поиска профиля; sparse пишется для схемы коллекции

    def __init__(
        self,
        model_id: str,
        *,
        dense_size: int,
        query_prefix: str = "",
        passage_prefix: str = "",
        device: str = "auto",
        batch_size: int = 8,
        max_length: int = 512,
    ) -> None:
        resolved_device = _resolve_device(device)
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbedderConfigError(
                "Dense-профиль требует опциональных зависимостей sentence-transformers+torch. "
                "Установите extra `embeddings` (uv: `uv sync --extra embeddings`), "
                "либо верните CC_EMBEDDER=hash. Базовый образ API намеренно без torch "
                "(docs/08 §4)."
            ) from exc
        self.model_id = model_id
        self.dense_size = dense_size
        self.device = resolved_device
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self._batch_size = batch_size
        self._max_length = max_length
        try:
            self._m = SentenceTransformer(model_id, device=resolved_device)
        except Exception as exc:  # сеть/кэш HF/несовместимые веса
            raise EmbedderConfigError(
                f"Не удалось загрузить эмбеддер '{model_id}' на устройстве "
                f"'{resolved_device}': {exc}. Проверьте кэш HF и сеть (модель качается "
                f"при первом запуске) или укажите CC_EMBEDDER_DEVICE=cpu."
            ) from exc
        self._m.max_seq_length = max_length
        self._lexical = HashEmbedder()  # sparse-канал схемы коллекции
        self._cache: dict[str, tuple[list[float], list[int], list[float]]] = {}

    def _encode(self, text: str, prefix: str) -> tuple[list[float], list[int], list[float]]:
        key = prefix + text
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        vec = self._m.encode(
            [prefix + text],
            batch_size=self._batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )[0]
        indices, values = self._lexical.sparse(text)
        encoded = (vec.tolist(), indices, values)
        if len(self._cache) >= _CACHE_SIZE:  # FIFO: держим память ограниченной
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = encoded
        return encoded

    def dense(self, text: str) -> list[float]:
        """Вектор ПАССАЖА (индексация) — с passage_prefix профиля."""
        return self._encode(text, self.passage_prefix)[0]

    def query_dense(self, text: str) -> list[float]:
        """Вектор ЗАПРОСА — с query_prefix профиля (инструкции e5/Qwen3)."""
        return self._encode(text, self.query_prefix)[0]

    def sparse(self, text: str) -> tuple[list[int], list[float]]:
        return self._encode(text, self.passage_prefix)[1:]


class RemoteEmbedder:
    """Эмбеддер ML-сайдкара на GPU через HTTP (docs/08 §4.7).

    Прод-образ API без torch: модель живёт в отдельном сервисе (`ml`), профиль
    эмбеддера передаётся как есть (`profile`). Размерность dense берётся из
    `/health` (или из первого ответа), несовпадение с ожидаемой профилем —
    громкая ошибка: молча обрезать вектор нельзя, коллекция Qdrant создаётся
    под конкретную размерность (docs/08 §3).

    Sparse-вектор лексический (hash) — нужен только для схемы коллекции;
    стратегия поиска профиля — dense-only (`use_sparse=False`).
    """

    use_sparse = False

    def __init__(self, profile_id: str, *, expected_dense_size: int = 0) -> None:
        from app.services.ml_client import MlUnavailable, get_ml_client

        client = get_ml_client()
        if not client.base_url:
            raise EmbedderConfigError(
                f"CC_EMBEDDER={profile_id} — профиль ML-сайдкара, но CC_ML_URL не задан. "
                "Укажите base-url сайдкара (compose: CC_ML_URL=http://ml:8080) "
                "или верните локальный профиль (hash/bge-m3/...)."
            )
        self.model_id = profile_id
        self.profile_id = profile_id
        self._client = client
        self._lexical = HashEmbedder()
        self._cache: dict[tuple[str, str], list[float]] = {}
        try:
            health = client.health()
        except MlUnavailable as exc:
            raise EmbedderConfigError(
                f"ML-сайдкар недоступен по CC_ML_URL={client.base_url}: {exc}. "
                "Поиск без эмбеддера невозможен — проверьте сервис `ml` (GET /health)."
            ) from exc
        models = health.get("models") or {}
        entry = models.get(profile_id) if isinstance(models, dict) else None
        size = 0
        if isinstance(entry, dict):
            size = int(entry.get("dense_size") or entry.get("dense") or 0)
        elif isinstance(entry, int):
            size = int(entry)
        if not size:
            # /health без размерности — узнаём из первого же ответа
            try:
                size = int(client.embeddings(["warmup"], kind="passage", profile=profile_id).get("dense_size") or 0)
            except MlUnavailable as exc:
                raise EmbedderConfigError(f"ML-сайдкар не вернул размерность для '{profile_id}': {exc}") from exc
        if not size:
            raise EmbedderConfigError(f"ML-сайдкар не сообщил dense_size для профиля '{profile_id}'")
        if expected_dense_size and size != expected_dense_size:
            raise EmbedderConfigError(
                f"Размерность эмбеддера '{profile_id}' в сайдкаре — {size}, а профиль ожидает "
                f"{expected_dense_size}. Коллекция Qdrant создаётся под одну размерность (docs/08 §3): "
                f"поправьте CC_ML_URL/профиль либо переиндексируйте KB под нужную модель."
            )
        self.dense_size = size

    def _vector(self, text: str, kind: str) -> list[float]:
        key = (kind, text)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        from app.services.ml_client import MlUnavailable

        data = self._client.embeddings([text], kind=kind, profile=self.profile_id)
        vectors = data.get("vectors") or []
        if not vectors or not isinstance(vectors[0], list):
            raise MlUnavailable("/v1/embeddings: пустой ответ")
        vec = [float(v) for v in vectors[0]]
        if len(vec) != self.dense_size:
            raise MlUnavailable(
                f"/v1/embeddings: размерность {len(vec)} != {self.dense_size} (профиль '{self.profile_id}')"
            )
        if len(self._cache) >= _CACHE_SIZE:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = vec
        return vec

    def dense(self, text: str) -> list[float]:
        """Вектор ПАССАЖА (индексация)."""
        return self._vector(text, "passage")

    def query_dense(self, text: str) -> list[float]:
        """Вектор ЗАПРОСА (асимметричные модели на сайдкаре применяют префиксы сами)."""
        return self._vector(text, "query")

    def sparse(self, text: str) -> tuple[list[int], list[float]]:
        return self._lexical.sparse(text)


def _resolve_device(device: str) -> str:
    """auto|cpu|cuda[:N] → конкретное устройство; понятная ошибка при промахе."""
    dev = (device or "auto").strip().lower()
    if dev == "auto":
        try:
            import torch
        except ImportError:
            return "cpu"  # FlagEmbedding всё равно потребует torch на загрузке
        return "cuda" if torch.cuda.is_available() else "cpu"
    if dev != "cpu" and not dev.startswith("cuda"):
        raise EmbedderConfigError(
            f"CC_EMBEDDER_DEVICE={device!r}: поддерживаются auto | cpu | cuda | cuda:N."
        )
    if dev.startswith("cuda"):
        try:
            import torch
        except ImportError as exc:
            raise EmbedderConfigError(
                f"CC_EMBEDDER_DEVICE={device!r} требует torch (extra `embeddings`). "
                "Поставьте зависимости или CC_EMBEDDER_DEVICE=cpu."
            ) from exc
        if not torch.cuda.is_available():
            raise EmbedderConfigError(
                f"CC_EMBEDDER_DEVICE={device!r}, но CUDA недоступна "
                "(torch.cuda.is_available() = False). Поставьте CC_EMBEDDER_DEVICE=cpu "
                "(bge-m3 на CPU ≈ 3 с/запрос, docs/07) или запустите на GPU-хосте."
            )
    return dev


def _dense_size(model) -> int:
    """Фактическая размерность dense-вектора загруженной модели."""
    inner = getattr(getattr(model, "model", None), "model", None)  # transformers
    size = getattr(getattr(inner, "config", None), "hidden_size", 0)
    if size:
        return int(size)
    out = model.encode(["probe"], return_dense=True, return_sparse=False, max_length=8)
    return len(out["dense_vecs"][0])


def build_embedder(
    *,
    kind: str | None = None,
    model_id: str | None = None,
    device: str | None = None,
    batch_size: int | None = None,
    max_length: int | None = None,
) -> Embedder:
    """Фабрика по CC_EMBEDDER: hash | bge-m3 (docs/08 §4).

    Аргументы переопределяют settings (CC_EMBEDDER_MODEL / _DEVICE / _BATCH_SIZE /
    _MAX_LENGTH) — нужно для bench/eval_app_path.py и тестов. Неизвестное
    значение CC_EMBEDDER → hash с предупреждением (совместимость демо).
    """
    from app.config import settings

    chosen = (kind if kind is not None else settings.embedder) or "hash"
    profile = PROFILES.get(chosen.strip().lower())
    if profile is None:
        log.warning(
            "CC_EMBEDDER=%r неизвестен; используется hash. Профили: %s",
            chosen,
            ", ".join(sorted(PROFILES)),
        )
        return HashEmbedder()
    if profile.kind == "bge-m3":
        return BgeM3Embedder(
            model_id or settings.embedder_model or profile.model_id,
            device=device or settings.embedder_device,
            batch_size=batch_size or settings.embedder_batch_size,
            max_length=max_length or settings.embedder_max_length,
        )
    if profile.kind == "st":
        return SentenceTransformerEmbedder(
            model_id or settings.embedder_model or profile.model_id,
            dense_size=profile.dense_size,
            query_prefix=profile.query_prefix,
            passage_prefix=profile.passage_prefix,
            device=device or settings.embedder_device,
            batch_size=batch_size or settings.embedder_batch_size,
            max_length=max_length or settings.embedder_max_length,
        )
    if profile.kind == "remote":
        # Модель живёт в ML-сайдкаре на GPU; локально torch не нужен.
        return RemoteEmbedder(profile.model_id, expected_dense_size=profile.dense_size)
    return HashEmbedder()


def profile_default_min_score(kind: str | None = None) -> float:
    """Порог «нет релевантного ответа» по умолчанию для выбранного профиля.

    CC_KB_MIN_SCORE (settings.kb_min_score) перекрывает его, если задан явно.
    """
    from app.config import settings

    chosen = (kind if kind is not None else settings.embedder) or "hash"
    profile = PROFILES.get(chosen.strip().lower())
    return profile.default_min_score if profile else 0.45


_embedder: Embedder | None = None
_embedder_lock = threading.Lock()


def get_embedder() -> Embedder:
    """Процессный синглтон эмбеддера: модель грузится один раз (старт приложения)."""
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                _embedder = build_embedder()
    return _embedder


def reset_embedder() -> None:
    """Сбросить синглтон (тесты; смена профиля без перезапуска процесса)."""
    global _embedder
    with _embedder_lock:
        _embedder = None


__all__ = [
    "Embedder",
    "EmbedderConfigError",
    "EmbedderProfile",
    "HashEmbedder",
    "BgeM3Embedder",
    "PROFILES",
    "build_embedder",
    "get_embedder",
    "reset_embedder",
    "DENSE_SIZE",
    "SPARSE_BUCKETS",
]
