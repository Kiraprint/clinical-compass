"""HTTP-клиент ML-сайдкара (docs/08 §4, docs/12).

GPU-сервис `ml` (compose) держит модели, которые нельзя тянуть в образ api:
эмбеддер БЗ, cross-encoder реранкер и NER-маскирование ПДн. Контракт — один
на всех потребителей, чтобы не появилось второго клиента:

- ``POST /v1/embeddings`` {texts, kind: "query"|"passage", profile}
  → {profile, dense_size, vectors}
- ``POST /v1/rerank`` {query, documents, top_k}
  → {scores} (порядок = порядок documents, больше — лучше)
- ``POST /v1/mask`` {text, names} → {text, spans, model}
- ``POST /v1/transcribe`` multipart {file, language}
  → {model, device, duration_s, language, segments: [{start, end, speaker, text}],
  speakers, degraded}
- ``GET /health`` → {status, device, models}

Функции синхронные: вызывающий уводит их в рабочий поток
(``anyio.to_thread``), как Qdrant/pywebpush. Никаких ретраев на бизнес-ошибки:
один повтор только на сетевой сбой/5xx — сайдкар идемпотентен.

Недоступность сайдкара — ``MlUnavailable``: решение (деградация или
fail-closed) принимает вызывающий, клиент молча не подменяет результат.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid

from app.config import settings

log = logging.getLogger(__name__)

# Таймаут на повтор: после первой ошибки ждать полный ml_timeout бессмысленно.
_RETRY_DELAY = 0.2
_MAX_BODY = 8 * 1024 * 1024  # потолок ответа: эмбеддинги пачки чанков
_MAX_NAMES = 128  # потолок deny-list в /v1/mask

# Предохранитель: если сайдкар лежит, каждый вызов иначе стоит два ml_timeout
# (по 30 с на маскирование), и любой ответ ассистента упирается в дедлайн.
# После _FAILURE_LIMIT сетевых сбоев подряд вызовы _COOLDOWN_S секунд падают
# сразу, без обращения к сети; первый успешный вызов сбрасывает счётчик.
_FAILURE_LIMIT = 3
_COOLDOWN_S = 30.0
_breaker_lock = threading.Lock()
_breaker_failures = 0
_breaker_open_until = 0.0


def _breaker_guard(path: str) -> None:
    """Полуоткрытое состояние: по истечении паузы пропускаем одну пробу."""
    global _breaker_open_until
    with _breaker_lock:
        if not _breaker_open_until:
            return
        now = time.monotonic()
        if now < _breaker_open_until:
            left = round(_breaker_open_until - now, 1)
            raise MlUnavailable(f"сайдкар {path}: недоступен, пауза ещё {left} с")
        _breaker_open_until = 0.0


def _breaker_ok() -> None:
    global _breaker_failures, _breaker_open_until
    with _breaker_lock:
        _breaker_failures = 0
        _breaker_open_until = 0.0


def _breaker_fail(path: str) -> None:
    global _breaker_failures, _breaker_open_until
    with _breaker_lock:
        _breaker_failures += 1
        if _breaker_failures >= _FAILURE_LIMIT:
            _breaker_open_until = time.monotonic() + _COOLDOWN_S
            log.warning(
                "ML-сайдкар: %d сбоев подряд (%s) — пауза %.0f с, вызовы отвечают MlUnavailable",
                _breaker_failures, path, _COOLDOWN_S,
            )


class MlUnavailable(RuntimeError):
    """Сайдкар не сконфигурирован, недоступен или ответил ошибкой."""

def enabled() -> bool:
    """Настроен ли сайдкар (CC_ML_URL). Пусто → все функции бросают MlUnavailable."""
    return bool(settings.ml_url.strip())

def _endpoint(path: str) -> str:
    base = settings.ml_url.strip().rstrip("/")
    if not base:
        raise MlUnavailable("CC_ML_URL не задан — ML-сайдкар не используется")
    return f"{base}{path}"

def _send(
    path: str,
    body: bytes,
    headers: dict[str, str],
    *,
    timeout: float | None = None,
    retries: int = 1,
) -> dict:
    """POST тела с `retries` повторами на сетевой сбой/5xx (по умолчанию один)."""
    _breaker_guard(path)
    headers = dict(headers)
    if settings.ml_token:
        headers["X-CC-ML-Token"] = settings.ml_token
    url = _endpoint(path)
    budget = settings.ml_timeout if timeout is None else timeout
    last: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=budget) as resp:
                raw = resp.read(_MAX_BODY)
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise MlUnavailable(f"сайдкар {path}: ожидался JSON-объект")
            _breaker_ok()
            return data
        except urllib.error.HTTPError as exc:
            # Тело ошибки не логируем и не тащим в текст исключения: сайдкар
            # обрабатывает ПДн, и его 4xx-диагностика может содержать фрагмент
            # входного текста. Наружу — только код и путь.
            last = MlUnavailable(f"сайдкар {path}: HTTP {exc.code}")
            if exc.code < 500:
                raise last from None  # 4xx — ошибка запроса, повтор не поможет
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = MlUnavailable(f"сайдкар {path}: {type(exc).__name__}")
        if attempt < retries:
            time.sleep(_RETRY_DELAY)
    assert last is not None
    _breaker_fail(path)
    raise last


def _post(path: str, payload: dict, *, timeout: float | None = None) -> dict:
    """POST JSON-объекта."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _send(path, body, {"Content-Type": "application/json"}, timeout=timeout)


def transcribe(
    data: bytes,
    *,
    filename: str = "audio.bin",
    mime: str = "application/octet-stream",
    language: str = "ru",
) -> dict:
    """Расшифровка аудио сессии (docs/18 §4.3, PR P3): multipart в сайдкар.

    Таймаут — `CC_SESSION_AI_TIMEOUT` (часовой приём расшифровывается минутами),
    повтора нет: после таймаута повторный прогон ASR только удвоит ожидание.
    Текст в исключения не попадает — только путь и HTTP-код.
    """
    boundary = uuid.uuid4().hex
    name = (filename or "audio.bin").replace('"', "") or "audio.bin"
    body = b"".join(
        [
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("utf-8"),
            bytes(data),
            (
                f"\r\n--{boundary}\r\n"
                'Content-Disposition: form-data; name="language"\r\n\r\n'
                f"{language}\r\n--{boundary}--\r\n"
            ).encode("utf-8"),
        ]
    )
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    result = _send(
        "/v1/transcribe", body, headers, timeout=settings.session_ai_timeout, retries=0
    )
    if not isinstance(result.get("segments"), list):
        raise MlUnavailable("сайдкар /v1/transcribe: ожидался список segments")
    return result

def ping() -> dict:
    """GET /health: статус моделей и устройство. Бросает MlUnavailable."""
    url = _endpoint("/health")
    headers = {}
    if settings.ml_token:
        headers["X-CC-ML-Token"] = settings.ml_token
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=settings.ml_timeout
        ) as resp:
            data = json.loads(resp.read(_MAX_BODY) or b"{}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise MlUnavailable(f"сайдкар /health: {exc}") from exc
    if not isinstance(data, dict):
        raise MlUnavailable("сайдкар /health: ожидался JSON-объект")
    _breaker_ok()  # живой ping — доказательство восстановления, цепь снова замкнута
    return data

def embed(texts: list[str], *, kind: str = "query", profile: str = "") -> list[list[float]]:
    """Векторы для текстов. kind: "query" (с префиксом запроса) | "passage"."""
    if not texts:
        return []
    data = _post(
        "/v1/embeddings",
        {"texts": list(texts), "kind": kind, "profile": profile},
    )
    vectors = data.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise MlUnavailable("сайдкар /v1/embeddings: число векторов не совпало с числом текстов")
    size = data.get("dense_size")
    for vec in vectors:
        if not isinstance(vec, list) or (size is not None and len(vec) != int(size)):
            raise MlUnavailable("сайдкар /v1/embeddings: размерность вектора не совпала с dense_size")
    return vectors

def rerank(query: str, documents: list[str], *, top_k: int | None = None) -> list[float]:
    """Скоры релевантности в порядке documents (больше — лучше)."""
    if not documents:
        return []
    payload: dict = {"query": query, "documents": list(documents)}
    if top_k is not None:
        payload["top_k"] = int(top_k)
    data = _post("/v1/rerank", payload)
    scores = data.get("scores")
    if not isinstance(scores, list) or len(scores) != len(documents):
        raise MlUnavailable("сайдкар /v1/rerank: число скоров не совпало с числом документов")
    return [float(s) for s in scores]

def mask(text: str, names: list[str] | None = None) -> dict:
    """Маскирование ПДн: {text, spans:[{category,start,end}], model}.

    Deny-list обрезается до 128 записей: у врача с большой выборкой ростер
    иначе раздувает каждый egress-запрос.
    """
    data = _post("/v1/mask", {"text": text, "names": list(names or [])[:_MAX_NAMES]})
    if not isinstance(data.get("text"), str) or not isinstance(data.get("spans"), list):
        raise MlUnavailable("сайдкар /v1/mask: ответ без text/spans")
    return data
