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
import io
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("ml.models")

# Потолки входа — общие для stub и реальных моделей (меняются только здесь,
# app.py берёт их из этого модуля).
MAX_TEXTS = 64
MAX_DOCUMENTS = 256
MAX_CHARS = 20_000
MAX_NAMES = 256

# --- ASR (faster-whisper) --------------------------------------------------
# Лид-решение по research-asr.md: один бэкенд — large-v3-turbo (MIT, VAD и
# PyAV внутри библиотеки, стабильный API). GigaAM-v3 точнее на русском
# (8.3% против 21% WER), но жёстко режет вход на 25-секундные куски и имеет
# молодые API — апгрейд-гейт описан в ml/README.md.
# CT2-конверсия MIT от Dropbox: `Systran/faster-whisper-large-v3-turbo` из
# research-asr.md на HF не существует (401), проверенный публичный аналог —
# этот репозиторий (3.1M загрузок, MIT, тот же large-v3-turbo в int8).
_ASR_MODEL = "dropbox-dash/faster-whisper-large-v3-turbo"
ASR_DEFAULT_LANGUAGE = "ru"
ASR_MAX_MB = 200
# Типы, которые реально присылают браузерный MediaRecorder и диктофоны
# (webm/ogg-opus, wav, m4a). Принимаем только их: остальное — 415.
ASR_MEDIA_TYPES = frozenset(
    {
        "audio/webm",
        "audio/ogg",
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/m4a",
        "audio/x-m4a",
    }
)
# Коды, которые понимает Whisper (faster_whisper.tokenizer._LANGUAGE_CODES).
# Список статический: в stub-режиме и в CI faster-whisper не установлен, а
# невалидный код иначе уходит в бэкенд и превращается в 500.
ASR_LANGUAGES = frozenset(
    """
    af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo
    fr gl gu ha haw he hi hr ht hu hy id is it ja jw ka kk km kn ko la lb ln lo
    lt lv mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si
    sk sl sn so sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo yue zh
    """.split()
)
# Оценка длительности в stub: 16 кГц, mono, int16 — без декодирования.
_ASR_STUB_BYTES_PER_S = 32_000
_ASR_STUB_SEGMENT_S = 5.0

# Окно NER: rubert видит 512 токенов, поэтому длинный текст режется на
# перекрывающиеся куски с привязкой к пробелам (см. HybridMasker).
NER_CHUNK_CHARS = 1200
NER_CHUNK_OVERLAP = 200

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


def public_model_name(model_id: str) -> str:
    """Имя модели для /health: файловый путь наружу не отдаём (только basename)."""
    value = (model_id or "").strip()
    if not value:
        return ""
    if value.startswith(("/", "~", ".")) or "\\" in value:
        return os.path.basename(value.rstrip("/\\"))
    return value


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
    # Лидер замеров поиска (docs/08 §4.7): dense-only Hit@3 0.968 на 600-символьных
    # чанках при 29–47 мс — лучший баланс «качество/скорость» без реранка.
    "bge-m3": EmbedProfile(
        model_id="BAAI/bge-m3",
        dense_size=1024,
        query_prefix="",
        passage_prefix="",
    ),
}

DEFAULT_PROFILE = "qwen3-0.6b"


class UnknownProfile(KeyError):
    """Профиль не сконфигурирован (CC_ML_PROFILES/встроенная таблица)."""


class ModelNotReady(RuntimeError):
    """Модели ещё грузятся или упали при загрузке."""


class ASRDisabled(RuntimeError):
    """ASR выключен конфигурацией (CC_ML_ASR=0 — дефолт)."""


class ASRUnavailable(RuntimeError):
    """Веса ASR не загрузились (текст ошибки остаётся в логах)."""


class ASRFailed(RuntimeError):
    """Бэкенд не смог обработать аудио (декодирование/распознавание)."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

def asr_max_mb() -> int:
    """Потолок аудио в МБ: CC_ML_ASR_MAX_MB (по умолчанию ASR_MAX_MB)."""
    raw = os.environ.get("CC_ML_ASR_MAX_MB", "").strip()
    if not raw:
        return ASR_MAX_MB
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"CC_ML_ASR_MAX_MB: ожидалось целое число ({raw!r})") from exc
    if value <= 0:
        raise RuntimeError("CC_ML_ASR_MAX_MB: должно быть больше нуля")
    return value

def asr_language(raw: str) -> str:
    """Код языка Whisper в нижнем регистре; незнакомый — ValueError."""
    value = (raw or "").strip().lower()
    if value not in ASR_LANGUAGES:
        raise ValueError(f"language {raw!r} не поддерживается: нужен код ISO (ru, en, ...)")
    return value

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


def _mask_payload(result: Any, *, model: str, degraded: bool) -> dict:
    return {
        "text": result.text,
        "spans": [
            {"category": f.kind, "start": f.start, "end": f.end}
            for f in result.findings
        ],
        "model": model,
        "degraded": degraded,
    }


def _regex_mask(
    text: str, names: list[str], *, model: str = "regex", degraded: bool = False
) -> dict:
    from compass.depersonalize import depersonalize

    return _mask_payload(
        depersonalize(text, names=names or None), model=model, degraded=degraded
    )


class RegexMasker:
    """Stub-маскер: только regex/валидаторы core, без NER."""

    def mask(self, text: str, names: list[str]) -> dict:
        return _regex_mask(text, names)

class StubTranscriber:
    """Stub ASR: длительность из размера файла, без декодирования и torch.

    Текст сегментов заведомо синтетический (префикс ``[stub]``): подставлять
    правдоподобную «расшифровку» в клинический путь нельзя. ``degraded`` у
    stub-пути всегда false — это полноценный (пусть и фиктивный) ответ
    backend'а, а не отказ.
    """

    device = "stub"

    def __init__(self) -> None:
        # Наружу — "stub", а не имя настроенной модели: ответ фиктивный.
        self.model_id = "stub"

    def transcribe(self, data: bytes, language: str) -> dict:
        duration = round(max(len(data) / _ASR_STUB_BYTES_PER_S, 0.5), 3)
        segments: list[dict[str, Any]] = []
        start = 0.0
        while start < duration:
            end = round(min(duration, start + _ASR_STUB_SEGMENT_S), 3)
            if end <= start:  # округление съело остаток — хвост не нужен
                break
            segments.append(
                {
                    "start": start,
                    "end": end,
                    "speaker": None,
                    "text": f"[stub] сегмент {len(segments) + 1}",
                }
            )
            start = end
        return {"duration_s": duration, "language": language, "segments": segments}

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
    тот же core-модуль, что и локальный гибрид.

    rubert видит 512 токенов (~1200 символов русского текста), поэтому
    длинный документ режется на перекрывающиеся окна по границам слов.
    Спан, оборванный правой границей окна, отбрасывается — его целиком
    увидит следующее окно (перекрытие 200 символов).

    Честность ответа: ``model="hybrid"`` только если NER реально отработал;
    при сбое любого окна ``_broken=True`` (до перезапуска), ответ —
    ``model="regex"``, ``degraded=true``, и api обязан считать это отказом
    backend'а (docs/12).
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

    @staticmethod
    def _chunks(text: str) -> list[tuple[int, int]]:
        """Окна (start, end) по границам слов с перекрытием NER_CHUNK_OVERLAP."""
        out: list[tuple[int, int]] = []
        start, size = 0, len(text)
        while start < size:
            end = min(start + NER_CHUNK_CHARS, size)
            if end < size:
                cut = text.rfind(" ", start + 1, end)
                if cut > start:
                    end = cut + 1
            out.append((start, end))
            if end >= size:
                break
            nxt = max(end - NER_CHUNK_OVERLAP, start + 1)
            space = text.find(" ", nxt, end)
            start = space + 1 if space > 0 else nxt
        return out

    def _fio_names(self, text: str) -> tuple[list[str], bool]:
        """(имена для deny-list, degraded). degraded=True — NER не отработал."""
        if not text:
            return [], False
        if self._broken:
            return [], True
        names: list[str] = []
        chunks = self._chunks(text)
        for index, (start, end) in enumerate(chunks):
            chunk = text[start:end]
            try:
                with self._lock:
                    entities = self._pipe(chunk)
            except Exception as exc:  # noqa: BLE001 — статус важнее, чем текст ошибки
                log.warning(
                    "rubert NER сбой (окно %d/%d): %r", index + 1, len(chunks), exc
                )
                self._broken = True
                return names, True
            last = index == len(chunks) - 1
            for entity in entities:
                label = entity.get("entity_group") or entity.get("entity")
                if _RUBERT_KIND.get(label) != "FIO":
                    continue
                ent_start = start + int(entity["start"])
                ent_end = start + int(entity["end"])
                if not last and ent_end >= end - 1:
                    continue  # оборван границей окна — покроет следующее
                value = text[ent_start:ent_end].strip()
                if value and value not in names:
                    names.append(value)
        return names, False

    def mask(self, text: str, names: list[str]) -> dict:
        found, degraded = self._fio_names(text)
        merged = list(names or [])
        for name in found:
            if name not in merged:
                merged.append(name)
        return _regex_mask(
            text,
            merged,
            model="regex" if degraded else "hybrid",
            degraded=degraded,
        )


class WhisperTranscriber:
    """faster-whisper large-v3-turbo (CTranslate2) с VAD внутри библиотеки.

    Внешний бинарь ffmpeg не нужен: колёса ``av`` несут собственные
    FFmpeg-библиотеки, а ``vad_filter=True`` (silero) выбрасывает тишину и
    режет длинную запись на 30-секундные окна — сессия на час проходит без
    ручной нарезки. ``transcribe`` возвращает ленивый генератор, поэтому
    итерируем под замком: CTranslate2 не потокобезопасен.
    """

    def __init__(
        self,
        model_id: str,
        device: str = "cuda",
        compute_type: str = "int8_float16",
    ) -> None:
        from faster_whisper import WhisperModel

        self.model_id = model_id
        self.device = device
        self.compute_type = compute_type
        self._model = WhisperModel(model_id, device=device, compute_type=compute_type)
        self._lock = threading.Lock()

    def transcribe(self, data: bytes, language: str) -> dict:
        with self._lock:
            segments, info = self._model.transcribe(
                io.BytesIO(data),
                language=language,
                beam_size=5,
                vad_filter=True,
                word_timestamps=False,
                condition_on_previous_text=False,
            )
            out: list[dict[str, Any]] = []
            for segment in segments:
                text = (segment.text or "").strip()
                start = round(float(segment.start), 3)
                end = round(float(segment.end), 3)
                if not text or end <= start:
                    continue
                out.append({"start": start, "end": end, "speaker": None, "text": text})
            duration = float(getattr(info, "duration", 0.0) or 0.0)
            detected = str(getattr(info, "language", "") or language)
        if duration <= 0.0:
            duration = out[-1]["end"] if out else 0.0
        return {"duration_s": duration, "language": detected, "segments": out}


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
        asr_model: str | None = None,
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
        self.asr_enabled = _env_bool("CC_ML_ASR")
        self.asr_model = (
            asr_model
            or os.environ.get("CC_ML_ASR_MODEL", "").strip()
            or _ASR_MODEL
        )
        # ASR можно увести на CPU отдельно от остальных моделей: замер дал
        # RTF 0.66 (8 потоков, int8) — 20-минутная сессия ≈ 13 мин, то есть
        # CPU-путь годится только для асинхронного фона.
        asr_device = os.environ.get("CC_ML_ASR_DEVICE", "").strip().lower()
        self.asr_device = (
            "stub"
            if self.stub
            else (self.device if asr_device in ("", "auto") else asr_device)
        )
        self.asr_compute = (
            os.environ.get("CC_ML_ASR_COMPUTE", "").strip()
            or ("int8_float16" if self.asr_device == "cuda" else "int8")
        )
        self._status = "loading"
        self._error: str | None = None
        self._lock = threading.Lock()
        self._warmup_lock = threading.Lock()
        self._embedders: dict[str, Any] = {}
        self._reranker: Any = None
        self._masker: Any = None
        self._started = False
        self._asr: Any = None
        self._asr_status = "idle"  # idle|loading|ready|error
        # Отдельный замок: ASR грузится лениво (десятки секунд для весов),
        # и /health не должен ждать загрузку, чтобы ответить status=ready.
        self._asr_lock = threading.Lock()

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
                    "rerank": (
                        public_model_name(self.rerank_model)
                        if self._reranker is not None
                        else None
                    ),
                    "mask": (
                        public_model_name(self.mask_model)
                        if self._masker is not None
                        else None
                    ),
                    "asr": {
                        "enabled": self.asr_enabled,
                        "status": self._asr_status,
                        "model": (
                            public_model_name(self.asr_model)
                            if self._asr is not None
                            else None
                        ),
                    },
                },
                # Никаких путей к весам/текстов ошибок наружу: только флаг.
                "error": self._status == "error",
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
        # ASR весит ~1.6 ГБ и грузится лениво: стартуем фоном уже после того,
        # как поиск готов, чтобы не задерживать статус ready.
        if self.asr_enabled:
            self._start_asr()

    def _require_ready(self) -> None:
        if not self.ready:
            # Текст ошибки загрузки остаётся в логах: в ответ он не попадает.
            raise ModelNotReady()

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

    # -- ASR ---------------------------------------------------------------

    def _start_asr(self) -> None:
        """Фоновая загрузка весов ASR ровно один раз.

        Быстрый путь без замка: во время самой загрузки `_asr_lock` держит
        загрузчик, и запрос обязан отвечать 503 сразу, а не ждать десятки
        секунд на этом замке.
        """
        if self._asr_status != "idle":
            return
        with self._asr_lock:
            if self._asr_status != "idle":
                return
            self._asr_status = "loading"
        threading.Thread(target=self._asr_thread, name="ml-asr", daemon=True).start()

    def _asr_thread(self) -> None:
        try:
            self.load_asr()
        except ASRUnavailable:
            # Статус уже выставлен в load_asr, текст ошибки — в логах.
            log.warning("ASR недоступен: транскрипция отключена до рестарта")

    def load_asr(self) -> Any:
        """Синхронная загрузка (warmup/тесты); single-flight под _asr_lock."""
        with self._asr_lock:
            if self._asr is not None:
                return self._asr
            if self._asr_status == "error":
                raise ASRUnavailable()
            self._asr_status = "loading"
            try:
                backend: Any = (
                    StubTranscriber()
                    if self.stub
                    else WhisperTranscriber(
                        self.asr_model, self.asr_device, self.asr_compute
                    )
                )
            except Exception as exc:  # noqa: BLE001 — текст ошибки не наружу
                log.exception("загрузка ASR упала")
                self._asr_status = "error"
                raise ASRUnavailable() from exc
            self._asr = backend
            self._asr_status = "ready"
            log.info(
                "ASR готов: model=%s device=%s compute=%s",
                public_model_name(self.asr_model),
                self.asr_device,
                self.asr_compute,
            )
            return self._asr

    def asr(self) -> Any:
        """Бэкенд ASR или причина 503 (loading/unavailable/disabled)."""
        if not self.asr_enabled:
            raise ASRDisabled()
        backend = self._asr
        if backend is not None:
            return backend
        status = self._asr_status
        if status == "error":
            raise ASRUnavailable()
        if status == "idle":
            self._start_asr()
        # "loading" — тоже сюда: 503 model_loading с Retry-After, без ожидания.
        raise ModelNotReady()

    def transcribe(self, data: bytes, language: str, *, diarize: bool = False) -> dict:
        self._require_ready()
        code = asr_language(language)
        backend = self.asr()
        started = time.perf_counter()
        try:
            raw = backend.transcribe(data, code)
        except Exception as exc:  # noqa: BLE001 — сбой бэкенда не должен быть 500
            log.exception("ASR не смог обработать аудио")
            raise ASRFailed() from exc
        elapsed = time.perf_counter() - started
        duration = float(raw.get("duration_s") or 0.0)
        segments = [
            {
                "start": round(float(segment["start"]), 3),
                "end": round(float(segment["end"]), 3),
                # Диаризация не реализована (pyannote gated) — спикер всегда null.
                "speaker": None,
                "text": str(segment["text"]),
            }
            for segment in raw.get("segments") or []
            if float(segment["end"]) > float(segment["start"])
        ]
        # Гарантия контракта: сегменты отсортированы и не пересекаются.
        segments.sort(key=lambda s: (s["start"], s["end"]))
        return {
            "model": public_model_name(backend.model_id),
            "device": backend.device,
            "duration_s": round(duration, 3),
            # RTF < 1 — быстрее реального времени; 0 при отсутствии аудио.
            "rtf": round(elapsed / duration, 4) if duration > 0 else 0.0,
            "language": str(raw.get("language") or code),
            "segments": segments,
            "speakers": [],
            # diarize=true без реализации — честный degraded, а не тихая подмена.
            "degraded": bool(diarize),
        }

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
