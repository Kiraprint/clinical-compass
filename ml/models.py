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
# Верхний предел CC_ML_ASR_MAX_MB: больше 200 МБ ≈ 3 ч opus не нужно, а
# 3-часовой файл всё равно декодируется в ~700 МБ float32 в памяти.
ASR_MAX_MB_CAP = 4096
# Типы, которые реально присылают браузерный MediaRecorder и диктофоны
# (webm/ogg-opus, wav, m4a). Принимаем только их: остальное — 415.
# ``video/*`` — те же контейнеры: у MediaRecorder с видеодорожкой и у любого
# прокси, определяющего тип по расширению (mimetypes: session.webm ->
# video/webm), звук лежит в WebM/MP4. Декодирование всё равно по содержимому,
# а нам нужна только аудиодорожка.
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
        "video/webm",
        "video/mp4",
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

# --- Диаризация (FR-42, docs/18 §4.5) --------------------------------------
# sherpa-onnx — обычное pip-колесо (CPU, свой onnxruntime внутри), веса —
# ONNX-файлы из GitHub releases k2-fsa, без HF-гейта:
#   pyannote/segmentation-3.0 (MIT) → границы реплик,
#   3D-Speaker CAM++ zh/en advanced (Apache-2.0) → эмбеддинги голоса,
#   кластеризация AHC внутри sherpa.
# Замер на 50.6 с синтетического русского диалога (2 голоса piper):
# RTF 0.08–0.12 (2 потока CPU, зависит от загрузки хоста), 12/12 реплик
# ml/README.md (раздел «Диаризация»). pyannote.audio не используется: их веса на HF gated.
DIARIZE_MODEL_DIR = "/models/diarize"
DIARIZE_SEGMENTATION_FILE = "segmentation.onnx"
DIARIZE_EMBEDDING_FILE = "embedding.onnx"
# Эмбеддинги sherpa считает по 16 кГц mono float32 — ровно тот формат, в
# котором декодирует PyAV, поэтому пересэмплировать ничего не нужно.
DIARIZE_SAMPLE_RATE = 16_000
# Клинический приём — два участника (врач и пациент): фиксированное число
# кластеров детерминированнее порога и не плодит «спикеров» из шума.
DIARIZE_CLUSTERS = 2
DIARIZE_MIN_DURATION_ON = 0.3
DIARIZE_MIN_DURATION_OFF = 0.5
# Stub-диаризация (CC_ML_STUB=1): чередование спикеров ровно на длину
# stub-сегмента, чтобы маппинг сегмент→спикер покрывался тестами без GPU.
_DIARIZE_STUB_TURN_S = 5.0
# Откуда берутся веса: релизы k2-fsa/sherpa-onnx на GitHub (pyannote.audio в
# HF-gated виде недоступен по лицензии). Хэши зафиксированы — прогрев качает
# файлы из сети, а офлайн-сайдкар потом сверяет их же.
DIARIZE_SEGMENTATION_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
# В архиве лежат model.onnx и model.int8.onnx; int8 на тесте схлопнул диалог в
# один кластер (6/12 реплик), поэтому берём fp32 (6 МБ).
DIARIZE_SEGMENTATION_SHA256 = (
    "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"
)
DIARIZE_SEGMENTATION_ARCHIVE_SHA256 = (
    "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"
)
DIARIZE_EMBEDDING_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
)
DIARIZE_EMBEDDING_SHA256 = (
    "aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2"
)
# Бэкенды диаризации за одним интерфейсом (`diarize(audio, duration_s)`):
#   sherpa     — дефолт: ONNX, CPU, веса из релизов k2-fsa (MIT/Apache-2.0);
#   sortformer — NVIDIA Sortformer v2.1 через NeMo, точнее и умеет 4 спикера,
#                но тянет свой torch (2.14+cu130) и 6.3 ГБ venv, поэтому в образ
#                ml не входит: включается CC_ML_DIARIZE_BACKEND=sortformer в
#                образе, где nemo_toolkit[asr] доустановлен. Без него — честная
#                деградация с кодом diarize_backend_unavailable.
DIARIZE_BACKENDS = ("sherpa", "sortformer")
DIARIZE_DEFAULT_BACKEND = "sherpa"
SORTFORMER_MODEL = "nvidia/diar_streaming_sortformer_4spk-v2.1"

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


class ASRBusy(RuntimeError):
    """Все слоты распознавания заняты: 503 busy + Retry-After."""


def asr_max_mb() -> int:
    """Потолок аудио в МБ: CC_ML_ASR_MAX_MB (по умолчанию ASR_MAX_MB).

    Кривое значение не роняет сайдкар: опечатка в env не должна утаскивать
    за собой эмбеддинги и маскирование (рестарт-луп контейнера). Возвращаем
    дефолт с предупреждением, сверху ограничиваем ASR_MAX_MB_CAP.
    """
    raw = os.environ.get("CC_ML_ASR_MAX_MB", "").strip()
    if not raw:
        return ASR_MAX_MB
    try:
        value = int(raw)
    except ValueError:
        log.warning("CC_ML_ASR_MAX_MB=%r: ожидалось целое число, беру %d", raw, ASR_MAX_MB)
        return ASR_MAX_MB
    if value <= 0:
        log.warning("CC_ML_ASR_MAX_MB=%d: должно быть больше нуля, беру %d", value, ASR_MAX_MB)
        return ASR_MAX_MB
    if value > ASR_MAX_MB_CAP:
        log.warning("CC_ML_ASR_MAX_MB=%d больше потолка %d", value, ASR_MAX_MB_CAP)
        return ASR_MAX_MB_CAP
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

    def transcribe(self, source: Any, language: str) -> dict:
        # Stub'у нужен только размер: у него нет декодера, читаем файл целиком.
        # Rewind обязателен: поток мог остаться на позиции после size-проверки
        # или второго вызова (реальный бэкенд делает то же самое).
        if hasattr(source, "seek"):
            source.seek(0)
        data = source if isinstance(source, bytes) else source.read()
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

    def transcribe(self, source: Any, language: str) -> dict:
        # Отдаём загрузку прямо в PyAV: файл не копируется в bytes, av читает
        # его покадрово (в память попадает только декодированное аудио).
        if hasattr(source, "seek"):
            source.seek(0)
        with self._lock:
            segments, info = self._model.transcribe(
                source,
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
# Диаризация (FR-42)
# ---------------------------------------------------------------------------


class DiarizeUnavailable(RuntimeError):
    """Моделей диаризации нет/они не открылись: путь деградации, не 5xx.

    ``code`` — причина для лога и статуса: ``diarize_unavailable`` (нет весов
    или библиотеки) и ``diarize_backend_unavailable`` (неизвестное значение
    ``CC_ML_DIARIZE_BACKEND``) — разные поломки, чинить их тоже по-разному.
    """

    def __init__(self, message: str, *, code: str = "diarize_unavailable") -> None:
        super().__init__(message)
        self.code = code


def diarize_labels(count: int) -> list[str]:
    """Индексы кластеров → ``A``, ``B``, … (роли назначает api, не сайдкар)."""
    return [chr(ord("A") + index) for index in range(max(0, count))]


def label_clusters(turns: list[dict]) -> dict[Any, str]:
    """Кластеры → метки в порядке первого появления (``A`` говорит первым)."""
    order: list[Any] = []
    for turn in sorted(turns, key=lambda item: (item["start"], item["end"])):
        if turn["speaker"] not in order:
            order.append(turn["speaker"])
    return dict(zip(order, diarize_labels(len(order))))


def assign_speakers(segments: list[dict], turns: list[dict]) -> list[Any]:
    """Сегмент → кластер поворота с максимальным перекрытием по времени.

    Сегмент без перекрытия (тишина/шум между репликами) получает ``None``:
    подставлять «соседнего» спикера было бы выдумкой.
    """
    assigned: list[Any] = []
    for segment in segments:
        start, end = float(segment["start"]), float(segment["end"])
        best: Any = None
        best_overlap = 0.0
        for turn in turns:
            overlap = min(end, float(turn["end"])) - max(start, float(turn["start"]))
            if overlap > best_overlap:
                best, best_overlap = turn["speaker"], overlap
        assigned.append(best)
    return assigned


class StubDiarizer:
    """Детерминированная заглушка: спикеры чередуются каждые 5 с.

    Настоящая модель в CI/тестах не грузится (GPU и 30 МБ весов), но маппинг
    сегмент→спикер и сборка ``speakers`` проверяются на реальном коде.
    """

    device = "stub"
    model_id = "stub"

    def diarize(self, audio: Any, duration_s: float) -> list[dict]:
        turns: list[dict] = []
        start = 0.0
        while start < duration_s:
            end = round(min(duration_s, start + _DIARIZE_STUB_TURN_S), 3)
            if end <= start:  # округление съело остаток — хвост не нужен
                break
            turns.append({"start": start, "end": end, "speaker": len(turns) % 2})
            start = end
        return turns


class SherpaDiarizer:
    """sherpa-onnx: pyannote segmentation 3.0 + 3D-Speaker CAM++ (CPU).

    Оба файла — ONNX из GitHub releases k2-fsa (MIT и Apache-2.0), лежат в
    ``CC_ML_DIARIZE_MODEL_DIR`` (том ml-models): офлайн, без HF-гейта.
    ``num_clusters`` фиксирован: приём ведёт врач с пациентом, а автопорог
    давал на тестовой записи третий «спикер» из шума. ``OfflineSpeakerDiarization``
    потокобезопасностью не славится (общий onnxruntime-сеанс) — держим замок.
    """

    device = "cpu"

    def __init__(
        self,
        model_dir: str,
        *,
        num_threads: int = 2,
        num_clusters: int = DIARIZE_CLUSTERS,
    ) -> None:
        import sherpa_onnx

        segmentation = os.path.join(model_dir, DIARIZE_SEGMENTATION_FILE)
        embedding = os.path.join(model_dir, DIARIZE_EMBEDDING_FILE)
        missing = [path for path in (segmentation, embedding) if not os.path.isfile(path)]
        if missing:
            raise DiarizeUnavailable(f"нет весов диаризации: {', '.join(missing)}")
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=segmentation
                ),
                num_threads=num_threads,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=embedding, num_threads=num_threads
            ),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_clusters),
            min_duration_on=DIARIZE_MIN_DURATION_ON,
            min_duration_off=DIARIZE_MIN_DURATION_OFF,
        )
        if not config.validate():
            raise DiarizeUnavailable(f"конфиг диаризации в {model_dir} не прошёл проверку")
        self._diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
        self._lock = threading.Lock()
        self.model_id = f"{DIARIZE_SEGMENTATION_FILE}+{DIARIZE_EMBEDDING_FILE}"

    def diarize(self, audio: Any, duration_s: float) -> list[dict]:
        with self._lock:
            result = self._diarizer.process(audio).sort_by_start_time()
        return [
            {
                "start": round(float(segment.start), 3),
                "end": round(float(segment.end), 3),
                "speaker": int(segment.speaker),
            }
            for segment in result
        ]


class SortformerDiarizer:
    """NVIDIA Sortformer v2.1 через NeMo: end-to-end, до 4 спикеров.

    Альтернативный бэкенд (``CC_ML_DIARIZE_BACKEND=sortformer``), не дефолт:
    ``nemo_toolkit[asr]`` приносит собственный torch (проверено на 2.14.1+cu130)
    и раздувает окружение до 6.3 ГБ, то есть живёт в отдельном образе, а не в
    ``ml/requirements.txt``. Замер на том же русском диалоге: сшивка 12/12
    реплик, RTF 0.33 на GPU (918 МБ VRAM), RAM-пик 2.5 ГБ на CPU — против
    RTF 0.08 у sherpa, поэтому дефолтом остаётся sherpa.
    """

    device = "torch"
    model_id = SORTFORMER_MODEL

    def __init__(self, model_id: str = SORTFORMER_MODEL) -> None:
        try:
            from nemo.collections.asr.models import SortformerEncLabelModel
        except ImportError as exc:  # нет nemo_toolkit в образе
            raise DiarizeUnavailable(f"nemo_toolkit не установлен: {exc}") from exc
        self._model = SortformerEncLabelModel.from_pretrained(model_id)
        self._model.eval()
        self.model_id = model_id
        self._lock = threading.Lock()

    def diarize(self, audio: Any, duration_s: float) -> list[dict]:
        with self._lock:
            batches = self._model.diarize(
                audio=[audio], batch_size=1, sample_rate=DIARIZE_SAMPLE_RATE
            )
        turns: list[dict] = []
        for line in batches[0]:
            start, end, speaker = line.split()
            # NeMo отдаёт метку строкой speaker_N — приводим к тем же int, что у
            # sherpa, чтобы маппинг на сегменты и метки A/B не зависели от бэкенда.
            turns.append(
                {
                    "start": round(float(start), 3),
                    "end": round(float(end), 3),
                    "speaker": int(speaker.rsplit("_", 1)[-1]),
                }
            )
        return turns


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
        # Слоты распознавания: каждая параллельная попытка — ещё один
        # декодированный в память файл (3 ч ≈ 700 МБ float32).
        try:
            slots = int(os.environ.get("CC_ML_ASR_CONCURRENCY", "1"))
        except ValueError:
            log.warning("CC_ML_ASR_CONCURRENCY=%r: ожидалось число, беру 1",
                        os.environ.get("CC_ML_ASR_CONCURRENCY"))
            slots = 1
        # Больше 4 параллельных распознаваний бессмысленны: каждое держит
        # декодированный файл в памяти, а CTranslate2 всё равно сериализует.
        self.asr_concurrency = min(max(1, slots), 4)
        self._asr_slots = threading.BoundedSemaphore(self.asr_concurrency)
        # Диаризация (FR-42): включена по умолчанию, но грузится лениво —
        # стеку без транскрипции 30 МБ весов в памяти не нужны. Ошибка
        # загрузки липкая (как у ASR): деградация вместо ретрая на каждом
        # запросе, причина — в логах один раз.
        self.diarize_enabled = _env_bool("CC_ML_DIARIZE", True)
        self.diarize_backend = (
            os.environ.get("CC_ML_DIARIZE_BACKEND", "").strip().lower()
            or DIARIZE_DEFAULT_BACKEND
        )
        self.diarize_model_dir = (
            os.environ.get("CC_ML_DIARIZE_MODEL_DIR", "").strip() or DIARIZE_MODEL_DIR
        )
        self._diarizer: Any = None
        self._diarizer_status = "idle"  # idle|ready|error
        self._diarizer_error: str | None = None
        self._diarizer_lock = threading.Lock()

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
                    # Диаризация грузится лениво; наружу — только статус и флаг.
                    "diarize": {
                        "enabled": self.diarize_enabled,
                        "status": self._diarizer_status,
                        "backend": "stub" if self.stub else self.diarize_backend,
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

    # -- диаризация --------------------------------------------------------

    def diarizer(self) -> Any:
        """Бэкенд диаризации или ``None`` — деградация без исключений.

        В отличие от ASR отсутствие весов не срывает запрос: расшифровка
        остаётся ценной сама по себе, поэтому ``diarize=true`` на сломанной
        модели отдаёт ``diarized=false, degraded=true`` (200), а не 503.
        """
        if not self.diarize_enabled:
            return None
        if self._diarizer_status == "ready":
            return self._diarizer
        if self._diarizer_status == "error":
            return None
        with self._diarizer_lock:
            if self._diarizer_status == "idle":
                try:
                    self._diarizer = self._build_diarizer()
                    self._diarizer_status = "ready"
                    log.info(
                        "Диаризация готова: бэкенд %s, модель %s",
                        self.diarize_backend,
                        self._diarizer.model_id,
                    )
                except Exception as exc:  # noqa: BLE001 — деградация, не падение
                    self._diarizer_status = "error"
                    code = getattr(exc, "code", type(exc).__name__)
                    # Причина — в лог один раз: дальше только флаг статуса.
                    self._diarizer_error = f"{code}: {exc}"[:300]
                    log.warning(
                        "Диаризация недоступна (бэкенд %s, каталог %s), "
                        "diarize=true будет деградировать: %s",
                        self.diarize_backend,
                        self.diarize_model_dir,
                        self._diarizer_error,
                    )
            return self._diarizer if self._diarizer_status == "ready" else None

    def _build_diarizer(self) -> Any:
        """Бэкенд по `CC_ML_DIARIZE_BACKEND`; неизвестное значение — деградация."""
        if self.stub:
            return StubDiarizer()
        if self.diarize_backend == "sherpa":
            return SherpaDiarizer(self.diarize_model_dir)
        if self.diarize_backend == "sortformer":
            return SortformerDiarizer()
        raise DiarizeUnavailable(
            f"неизвестный бэкенд {self.diarize_backend!r}: {', '.join(DIARIZE_BACKENDS)}",
            code="diarize_backend_unavailable",
        )

    def _diarize_turns(self, source: Any, duration_s: float) -> dict:
        """Повороты спикеров для транскрипции; никогда не бросает.

        Возвращает ``{turns, diarized, degraded, reason}``: ``reason`` уходит
        в лог (в ответе — только флаги, без путей к весам).
        """
        if not self.diarize_enabled:
            return {"turns": [], "diarized": False, "degraded": False, "reason": "disabled"}
        backend = self.diarizer()
        if backend is None:
            return {
                "turns": [],
                "diarized": False,
                "degraded": True,
                "reason": self._diarizer_error or "unavailable",
            }
        started = time.perf_counter()
        try:
            audio: Any = None
            if not isinstance(backend, StubDiarizer):
                # Тот же декодер PyAV, что у Whisper: 16 кГц mono float32 —
                # формат, который ждут эмбеддинги sherpa.
                from faster_whisper.audio import decode_audio

                if hasattr(source, "seek"):
                    source.seek(0)
                audio = decode_audio(source, sampling_rate=DIARIZE_SAMPLE_RATE)
            turns = backend.diarize(audio, duration_s)
        except Exception:  # noqa: BLE001 — сбой диаризации не должен ронять ASR
            log.exception("Диаризация не удалась, сегменты останутся без спикеров")
            return {
                "turns": [],
                "diarized": False,
                "degraded": True,
                "reason": "failed",
            }
        if not turns:
            log.warning("Диаризация не нашла реплик: сегменты останутся без спикеров")
            return {
                "turns": [],
                "diarized": False,
                "degraded": True,
                "reason": "no_turns",
            }
        elapsed = time.perf_counter() - started
        log.info(
            "Диаризация: %d поворотов, %d спикеров, %.2f с на %.1f с аудио",
            len(turns),
            len({turn["speaker"] for turn in turns}),
            elapsed,
            duration_s,
        )
        return {"turns": turns, "diarized": True, "degraded": False, "reason": None}

    def transcribe(self, source: Any, language: str, *, diarize: bool = False) -> dict:
        self._require_ready()
        code = asr_language(language)
        backend = self.asr()
        # Не ждём слот: api ждёт ответа не дольше CC_ML_TIMEOUT, а «в очереди
        # на 10 минут» — худший ответ, чем честный 503 busy с Retry-After.
        if not self._asr_slots.acquire(blocking=False):
            raise ASRBusy()
        started = time.perf_counter()
        try:
            try:
                raw = backend.transcribe(source, code)
            except Exception as exc:  # noqa: BLE001 — сбой бэкенда не должен быть 500
                log.exception("ASR не смог обработать аудио")
                raise ASRFailed() from exc
            duration = float(raw.get("duration_s") or 0.0)
            segments = [
                {
                    "start": round(float(segment["start"]), 3),
                    "end": round(float(segment["end"]), 3),
                    # Спикера выставит диаризация ниже; без неё — честный null.
                    "speaker": None,
                    "text": str(segment["text"]),
                }
                for segment in raw.get("segments") or []
                if float(segment["end"]) > float(segment["start"])
            ]
            # Гарантия контракта: сегменты отсортированы и не пересекаются.
            segments.sort(key=lambda s: (s["start"], s["end"]))
            # Диаризация — под тем же слотом: она тоже держит декодированное
            # аудио в памяти (3 ч ≈ 700 МБ float32) и грузит CPU.
            diarization = (
                self._diarize_turns(source, duration)
                if diarize
                else {"turns": [], "diarized": False, "degraded": False, "reason": "disabled"}
            )
        finally:
            self._asr_slots.release()
        elapsed = time.perf_counter() - started
        speakers: list[dict] = []
        if diarization["diarized"]:
            labels = label_clusters(diarization["turns"])
            totals: dict[str, dict] = {}
            assigned = assign_speakers(segments, diarization["turns"])
            for segment, cluster in zip(segments, assigned):
                label = labels.get(cluster) if cluster is not None else None
                segment["speaker"] = label
                if label is None:
                    continue
                item = totals.setdefault(
                    label, {"id": label, "label": None, "seconds": 0.0, "segments": 0}
                )
                item["seconds"] = round(
                    item["seconds"] + max(0.0, segment["end"] - segment["start"]), 3
                )
                item["segments"] += 1
            speakers = [totals[label] for label in sorted(totals)]
        return {
            "model": public_model_name(backend.model_id),
            "device": backend.device,
            "duration_s": round(duration, 3),
            # RTF < 1 — быстрее реального времени (ASR + диаризация); 0 без аудио.
            "rtf": round(elapsed / duration, 4) if duration > 0 else 0.0,
            "language": str(raw.get("language") or code),
            "segments": segments,
            "speakers": speakers,
            # diarized=false — честная деградация (весов нет/упало/выключено),
            # а не тихая подмена: api покажет, что говорящие не разделены.
            "diarized": bool(diarization["diarized"]),
            "degraded": bool(diarization["degraded"]),
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
