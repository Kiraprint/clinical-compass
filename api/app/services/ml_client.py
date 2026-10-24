"""HTTP-клиент ML-сайдкара на GPU (docs/08 §4.7).

Прод-образ API не содержит torch, а эмбеддинг и реранк требуют модели.
Поэтому тяжёлые модели живут в отдельном сервисе (`ml` в compose, nvidia
runtime), а API ходит к нему по HTTP. Контракт заморожен и общий для всех
потребителей (эмбеддер БЗ, реранк, NER-mask):

  POST {CC_ML_URL}/v1/embeddings  {"texts", "kind": query|passage, "profile"}
       → {"profile", "dense_size", "vectors": [[float, ...], ...]}
  POST {CC_ML_URL}/v1/rerank      {"query", "documents", "top_k"}
       → {"scores": [float, ...]}   (порядок совпадает с documents)
  POST {CC_ML_URL}/v1/mask        {"text", "names"}
       → {"text", "spans": [{category, start, end}], "model"}
  GET  {CC_ML_URL}/health         → {"status", "device", "models"}

Авторизация — необязательный заголовок `X-CC-ML-Token` (`CC_ML_TOKEN`).

Модуль намеренно без внешних зависимостей: `urllib` из stdlib, как у
остальных HTTP-вызовов API (LLM-шлюз). Все ошибки транспорта/ответа —
`MlUnavailable`; вызывающая сторона решает, деградировать или падать.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from app.config import settings

# Таймаут по умолчанию: короткий, чтобы поиск не висел на упавшем сайдкаре.
DEFAULT_TIMEOUT_S = 5.0


class MlUnavailable(RuntimeError):
    """Сайдкар недоступен или ответил некорректно."""


class MlClient:
    """Минимальный клиент контракта ML-сайдкара."""

    def __init__(self, base_url: str, *, token: str = "", timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout_s = timeout_s

    # --- транспорт ------------------------------------------------------
    def _post(self, path: str, payload: dict, *, timeout_s: float | None = None) -> dict:
        if not self.base_url:
            raise MlUnavailable("CC_ML_URL не задан")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json", **({"X-CC-ML-Token": self.token} if self.token else {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as resp:  # noqa: S310 — внутренний сервис
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            raise MlUnavailable(f"{path}: {exc}") from exc
        if not isinstance(data, dict):
            raise MlUnavailable(f"{path}: ответ не JSON-объект")
        return data

    def _get(self, path: str, *, timeout_s: float | None = None) -> dict:
        if not self.base_url:
            raise MlUnavailable("CC_ML_URL не задан")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={"Accept": "application/json", **({"X-CC-ML-Token": self.token} if self.token else {})},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            raise MlUnavailable(f"{path}: {exc}") from exc
        if not isinstance(data, dict):
            raise MlUnavailable(f"{path}: ответ не JSON-объект")
        return data

    # --- API контракта --------------------------------------------------
    def health(self, *, timeout_s: float | None = None) -> dict:
        return self._get("/health", timeout_s=timeout_s)

    def embeddings(
        self,
        texts: list[str],
        *,
        kind: str = "query",
        profile: str = "",
        timeout_s: float | None = None,
    ) -> dict:
        if kind not in {"query", "passage"}:
            raise ValueError(f"kind должен быть query|passage, получено {kind!r}")
        return self._post(
            "/v1/embeddings",
            {"texts": list(texts), "kind": kind, "profile": profile},
            timeout_s=timeout_s,
        )

    def rerank(self, query: str, documents: list[str], *, top_k: int = 0, timeout_s: float | None = None) -> list[float]:
        data = self._post(
            "/v1/rerank",
            {"query": query, "documents": list(documents), "top_k": int(top_k)},
            timeout_s=timeout_s,
        )
        scores = data.get("scores")
        if not isinstance(scores, list) or len(scores) != len(documents):
            raise MlUnavailable(f"/v1/rerank: ожидался список из {len(documents)} чисел, получено {scores!r}")
        try:
            return [float(s) for s in scores]
        except (TypeError, ValueError) as exc:
            raise MlUnavailable(f"/v1/rerank: нечисловые скоры ({exc})") from exc

    def mask(self, text: str, *, names: list[str] | None = None, timeout_s: float | None = None) -> dict:
        return self._post("/v1/mask", {"text": text, "names": list(names or [])}, timeout_s=timeout_s)


_client: MlClient | None = None
_lock = threading.Lock()


def get_ml_client() -> MlClient:
    """Процессный синглтон клиента (URL/токен фиксируются на первом обращении)."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = MlClient(
                    getattr(settings, "ml_url", "") or "",
                    token=getattr(settings, "ml_token", "") or "",
                    timeout_s=float(getattr(settings, "ml_timeout_s", DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S),
                )
    return _client


def reset_ml_client() -> None:
    """Сбросить синглтон (тесты/смена CC_ML_URL без перезапуска процесса)."""
    global _client
    with _lock:
        _client = None
