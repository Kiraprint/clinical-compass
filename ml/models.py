"""Реестр моделей ML-сайдкара (docs/08 §4, docs/12).

Один процесс держит три модели и грузит их лениво/в фоне:
- эмбеддер (`sentence-transformers`, асимметричные префиксы запрос/пассаж);
- cross-encoder реранкер (`BAAI/bge-reranker-v2-m3`, сырые логиты);
- маскер ПДн: гибрид `compass.depersonalize` (regex + валидаторы) и
  rubert token-classifier (`redmadrobot-rnd/rubert-base-pii-ner`) поверх той же
  deny-list-расшифровки, что и в api (`app.services.depersonalize`), чтобы
  прод-спаны совпадали с локальным гибридным бэкендом.

`CC_ML_STUB=1` — детерминированный stub без torch: тесты и CI без GPU.
Формы ответов у stub и настоящих моделей одинаковые.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("ml.models")

# Потолки входа — общие для stub и реальных моделей (меняются только здесь,
# app.py берёт их из этого модуля).
MAX_TEXTS = 64
MAX_DOCUMENTS = 256
MAX_CHARS = 20_000
MAX_NAMES = 256

# Инструкция запроса для instruct-эмбеддеров. Модели обучены на английских
# инструкциях; сам текст запроса/пассажа остаётся русским.
DEFAULT_INSTRUCTION = (
    "Given a clinical knowledge-base query, retrieve relevant medical passages "
    "that answer the query"
)

_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
_NER_MODEL = "redmadrobot-rnd/rubert-base-pii-ner"

# Категории rubert-base-pii-ner -> виды гварда core/compass/depersonalize
# (копия таблицы api/app/services/depersonalize.py: образ ml не содержит api).
_RUBERT_KIND = {
    "FIRST_NAME": "FIO",
    "LAST_NAME": "FIO",
    "MIDDLE_NAME": "FIO",
    "NAME": "FIO",
    "CITY": "ADDRESS",
    "STREET": "ADDRESS",
    "HOUSE": "ADDRESS",
    "REGION": "ADDRESS",
    "DISTRICT": "ADDRESS",
    "COUNTRY": "ADDRESS",
    "PASSPORT": "DOC",
    "SNILS": "DOC",
    "INN": "DOC",
    "OMS": "DOC",
    "DRIVER_LICENSE": "DOC",
    "MILITARY_ID": "DOC",
    "BIRTH_CERTIFICATE": "DOC",
    "CREDIT_CARD": "DOC",
    "PHONE": "PHONE",
    "EMAIL": "EMAIL",
}


@dataclass(frozen=True)
class EmbedProfile:
    """Профиль эмбеддингов: модель, размерность и префиксы.

    ``query_prefix``/``passage_prefix`` — шаблоны; ``{instruction}``
    подставляется из ``CC_ML_QUERY_INSTRUCTION``. Асимметрия обязательна:
    у instruct-моделей пассажи идут без префикса, запросы — с инструкцией.
    """

    model_id: str
    dense_size: int
    query_prefix: str = ""
    passage_prefix: str = ""

    def prefix(self, kind: str, instruction: str) -> str:
        template = self.query_prefix if kind == "query" else self.passage_prefix
        return template.replace("{instruction}", instruction)


# Встроенные профили. Дефолт выбирается одной env CC_ML_DEFAULT_PROFILE
# (замер качества/скорости на этом наборе — REPORT-ml-sidecar.md).
BUILTIN_PROFILES: dict[str, EmbedProfile] = {
    "qwen3-0.6b": EmbedProfile(
        model_id="Qwen/Qwen3-Embedding-0.6B",
        dense_size=1024,
        query_prefix="Instruct: {instruction}\nQuery: ",
        passage_prefix="",
    ),
    "e5-large-instruct": EmbedProfile(
        model_id="intfloat/multilingual-e5-large-instruct",
        dense_size=1024,
        query_prefix="Instruct: {instruction}\nQuery: ",
        passage_prefix="",
    ),
}

DEFAULT_PROFILE = "qwen3-0.6b"


class UnknownProfile(KeyError):
    """Профиль не сконфигурирован (CC_ML_PROFILES/встроенная таблица)."""


class ModelNotReady(RuntimeError):
    """Модели ещё грузятся или упали при загрузке."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_profiles() -> dict[str, EmbedProfile]:
    """Встроенные профили + переопределения из CC_ML_PROFILES (JSON).

    Формат: ``{"name": {"model": "...", "dense_size": 1024,
    "query_prefix": "...", "passage_prefix": "..."}}``; встроенные поля
    можно переопределять частично.
    """
    profiles = dict(BUILTIN_PROFILES)
    raw = os.environ.get("CC_ML_PROFILES", "").strip()
    if not raw:
        return profiles
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CC_ML_PROFILES: некорректный JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise RuntimeError("CC_ML_PROFILES: ожидался JSON-объект {имя: профиль}")
    for key, value in data.items():
        if not isinstance(value, dict):
            raise RuntimeError(f"CC_ML_PROFILES[{key!r}]: ожидался объект")
        base = profiles.get(key)
        model_id = str(value.get("model") or (base.model_id if base else ""))
        if not model_id:
            raise RuntimeError(f"CC_ML_PROFILES[{key!r}]: нет model")
        dense_size = int(value.get("dense_size") or (base.dense_size if base else 0))
        if dense_size <= 0:
            raise RuntimeError(f"CC_ML_PROFILES[{key!r}]: нет dense_size")
        profiles[key] = EmbedProfile(
            model_id=model_id,
            dense_size=dense_size,
            query_prefix=str(
                value.get("query_prefix", base.query_prefix if base else "")
            ),
            passage_prefix=str(
                value.get("passage_prefix", base.passage_prefix if base else "")
            ),
        )
    return profiles


# ---------------------------------------------------------------------------
# Stub-бэкенды: детерминированно, без torch и без токенизаторов.
# ---------------------------------------------------------------------------


class HashEmbedder:
    """Хешевый эмбеддер в форме настоящего: L2-норма, размерность профиля."""

    def __init__(self, profile: EmbedProfile) -> None:
        self.profile = profile

    def encode(self, texts: list[str], kind: str, instruction: str) -> list[list[float]]:
        dim = self.profile.dense_size
        prefix = self.profile.prefix(kind, instruction)
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * dim
            for token in re.findall(r"\w+", (prefix + text).lower()):
                digest = hashlib.sha256(f"{self.profile.model_id}:{token}".encode()).digest()
                idx = int.from_bytes(digest[:4], "big") % dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[idx] += sign
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class KeywordReranker:
    """Ключевой пересекающийся реранкер: больше общих слов — выше скор."""

    def predict(self, query: str, documents: list[str]) -> list[float]:
        q = set(re.findall(r"\w+", query.lower()))
        scores: list[float] = []
        for doc in documents:
            d = set(re.findall(r"\w+", doc.lower()))
            overlap = len(q & d)
            tie = int.from_bytes(hashlib.sha256(doc.encode()).digest()[:2], "big") / 65536.0
            scores.append(min(1.0, overlap / max(1, len(q))) + tie * 1e-6)
        return scores


def _regex_mask(text: str, names: list[str]) -> dict:
    from compass.depersonalize import depersonalize

    result = depersonalize(text, names=names or None)
    return {
        "text": result.text,
        "spans": [
            {"category": f.kind, "start": f.start, "end": f.end}
            for f in result.findings
        ],
        "model": "hybrid",
    }


class RegexMasker:
    """Stub-маскер: только regex/валидаторы core, без NER."""

    def mask(self, text: str, names: list[str]) -> dict:
        return _regex_mask(text, names)


# ---------------------------------------------------------------------------
# Настоящие модели (ленивый импорт torch/transformers).
# ---------------------------------------------------------------------------


def resolve_device(requested: str) -> str:
    """CC_ML_DEVICE=auto|cuda|cpu -> "cuda"|"cpu"; без CUDA cuda недоступен."""
    want = (requested or "auto").strip().lower()
    if want in ("cpu",):
        return "cpu"
    if want.startswith("cuda"):
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
        except Exception:  # noqa: BLE001 — нет torch == нет CUDA
            pass
        log.warning("CC_ML_DEVICE=%s, но CUDA недоступна — работаю на CPU", requested)
        return "cpu"
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"


class StEmbedder:
    """sentence-transformers + bf16 на CUDA, асимметричные префиксы профиля."""

    def __init__(self, profile: EmbedProfile, device: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.profile = profile
        self.device = device
        self._model = SentenceTransformer(profile.model_id, device=device)
        if device == "cuda":
            self._model.bfloat16()
        self._lock = threading.Lock()

    def encode(self, texts: list[str], kind: str, instruction: str) -> list[list[float]]:
        prefix = self.profile.prefix(kind, instruction)
        inputs = [prefix + t for t in texts]
        with self._lock:
            vectors = self._model.encode(
                inputs,
                batch_size=16 if self.device == "cuda" else 8,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        return [[float(x) for x in row] for row in vectors]


class StReranker:
    """CrossEncoder bge-reranker-v2-m3: сырые логиты, обрезка на 512 токенах."""

    def __init__(self, model_id: str, device: str) -> None:
        from sentence_transformers import CrossEncoder

        self.model_id = model_id
        self.device = device
        self._model = CrossEncoder(model_id, device=device, max_length=512)
        if device == "cuda":
            self._model.model.bfloat16()
        self._lock = threading.Lock()

    def predict(self, query: str, documents: list[str]) -> list[float]:
        pairs = [(query, doc) for doc in documents]
        with self._lock:
            scores = self._model.predict(
                pairs,
                batch_size=16 if self.device == "cuda" else 8,
                show_progress_bar=False,
            )
        return [float(s) for s in scores]


class HybridMasker:
    """rubert token-classifier -> deny-list для core/compass/depersonalize.

    Повторяет api/app/services/depersonalize.py (aggregation_strategy="first",
    только FIO-спаны идут в deny-list); маскирование и границы спанов считает
    тот же core-модуль, что и локальный гибрид — спаны совпадают.
    """

    def __init__(self, model_id: str, device: str) -> None:
        import torch
        from transformers import pipeline

        self.model_id = model_id
        self._pipe = pipeline(
            "token-classification",
            model=model_id,
            device=0 if device == "cuda" else -1,
            dtype=torch.bfloat16 if device == "cuda" else torch.float32,
            aggregation_strategy="first",
        )
        self._lock = threading.Lock()
        self._broken = False

    def _names(self, text: str) -> list[str]:
        if self._broken or not text:
            return []
        try:
            with self._lock:
                entities = self._pipe(text)
        except Exception as exc:  # noqa: BLE001 — NER не должен валить маскирование
            log.warning("rubert NER сбой: %s", exc)
            return []
        names: list[str] = []
        for entity in entities:
            label = entity.get("entity_group") or entity.get("entity")
            if _RUBERT_KIND.get(label) == "FIO":
                value = text[int(entity["start"]) : int(entity["end"])].strip()
                if value and value not in names:
                    names.append(value)
        return names

    def mask(self, text: str, names: list[str]) -> dict:
        merged = list(names or [])
        for name in self._names(text):
            if name not in merged:
                merged.append(name)
        return _regex_mask(text, merged)


# ---------------------------------------------------------------------------
# Реестр
# ---------------------------------------------------------------------------


class Registry:
    """Держит бэкенды и статус загрузки; потокобезопасен."""

    def __init__(
        self,
        *,
        stub: bool | None = None,
        device: str | None = None,
        profiles: dict[str, EmbedProfile] | None = None,
        default_profile: str | None = None,
        instruction: str | None = None,
        rerank_model: str | None = None,
        mask_model: str | None = None,
    ) -> None:
        self.stub = _env_bool("CC_ML_STUB") if stub is None else stub
        self.profiles = load_profiles() if profiles is None else dict(profiles)
        self.default_profile = (
            default_profile
            or os.environ.get("CC_ML_DEFAULT_PROFILE", "").strip()
            or DEFAULT_PROFILE
        )
        self.instruction = (
            instruction
            or os.environ.get("CC_ML_QUERY_INSTRUCTION", "").strip()
            or DEFAULT_INSTRUCTION
        )
        self.rerank_model = rerank_model or os.environ.get(
            "CC_ML_RERANK_MODEL", _RERANK_MODEL
        )
        self.mask_model = mask_model or os.environ.get("CC_ML_NER_MODEL", _NER_MODEL)
        if self.default_profile not in self.profiles:
            raise RuntimeError(
                f"CC_ML_DEFAULT_PROFILE={self.default_profile!r} отсутствует в профилях "
                f"({', '.join(sorted(self.profiles))})"
            )
        self.device = "stub" if self.stub else resolve_device(device or os.environ.get("CC_ML_DEVICE", "auto"))
        self._status = "loading"
        self._error: str | None = None
        self._lock = threading.Lock()
        self._warmup_lock = threading.Lock()
        self._embedders: dict[str, Any] = {}
        self._reranker: Any = None
        self._masker: Any = None
        self._started = False

    # -- статус ------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._status == "ready"

    def status(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "device": self.device,
                "stub": self.stub,
                "models": {
                    "embeddings": {
                        "default": self.default_profile,
                        "loaded": sorted(self._embedders),
                        "available": sorted(self.profiles),
                    },
                    "rerank": self.rerank_model if self._reranker is not None else None,
                    "mask": self.mask_model if self._masker is not None else None,
                },
                "error": self._error,
            }

    # -- загрузка ----------------------------------------------------------

    def start(self) -> None:
        """Фоновая загрузка моделей (lifespan приложения)."""
        with self._warmup_lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._warmup, name="ml-warmup", daemon=True).start()

    def warmup(self) -> None:
        """Синхронная загрузка (тесты/пробы); повторный вызов — no-op."""
        self._warmup()

    def _warmup(self) -> None:
        with self._warmup_lock:
            if self._status != "loading":
                return
            try:
                self.embedder(self.default_profile)
                self.reranker()
                self.masker()
            except Exception as exc:  # noqa: BLE001 — статус читается /health
                log.exception("загрузка моделей упала")
                with self._lock:
                    self._status = "error"
                    self._error = f"{type(exc).__name__}: {exc}"
                return
            with self._lock:
                self._status = "ready"
                self._error = None
        log.info("модели готовы: device=%s profile=%s", self.device, self.default_profile)

    def _require_ready(self) -> None:
        if not self.ready:
            raise ModelNotReady(self._error or "модели загружаются")

    # -- бэкенды -----------------------------------------------------------

    def embedder(self, profile_key: str) -> Any:
        key = (profile_key or "").strip() or self.default_profile
        if key not in self.profiles:
            raise UnknownProfile(key)
        with self._lock:
            cached = self._embedders.get(key)
        if cached is not None:
            return cached
        profile = self.profiles[key]
        backend = HashEmbedder(profile) if self.stub else StEmbedder(profile, self.device)
        with self._lock:
            self._embedders.setdefault(key, backend)
            return self._embedders[key]

    def reranker(self) -> Any:
        with self._lock:
            if self._reranker is None:
                self._reranker = (
                    KeywordReranker()
                    if self.stub
                    else StReranker(self.rerank_model, self.device)
                )
            return self._reranker

    def masker(self) -> Any:
        with self._lock:
            if self._masker is None:
                self._masker = (
                    RegexMasker()
                    if self.stub
                    else HybridMasker(self.mask_model, self.device)
                )
            return self._masker

    # -- операции ----------------------------------------------------------

    def embed(self, texts: list[str], kind: str, profile: str) -> tuple[str, int, list[list[float]]]:
        self._require_ready()
        key = (profile or "").strip() or self.default_profile
        backend = self.embedder(key)
        vectors = backend.encode(texts, kind, self.instruction)
        return key, self.profiles[key].dense_size, vectors

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        self._require_ready()
        return self.reranker().predict(query, documents)

    def mask(self, text: str, names: list[str]) -> dict:
        self._require_ready()
        result = self.masker().mask(text, names)
        # Гарантия контракта: спаны отсортированы по началу (core уже сортирует,
        # но stub/будущие бэкенды не обязаны).
        result["spans"].sort(key=lambda s: (s["start"], s["end"]))
        return result
