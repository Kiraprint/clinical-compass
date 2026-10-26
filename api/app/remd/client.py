"""HTTP-клиент РЭМД (FR-41, docs/18 §8). Только stdlib: urllib/json/base64.

Контракт тот же, что у `app.emias.client` (docs/10 R17-R19):

  * белый список хостов: каждый запрос сверяется с netloc `CC_REMD_BASE_URL`,
    redirect на другой хост запрещён (urllib не следует редиректам);
  * OAuth `client_credentials`, токен кэшируется и обновляется на 80% TTL,
    повторный 401 → один принудительный refresh;
  * до `CC_REMD_MAX_ATTEMPTS` попыток на 429/5xx/таймаут с экспоненциальным
    backoff и jitter; `Retry-After` соблюдается; 4xx не ретраятся никогда;
  * жёсткий кап тела ответа (`CC_REMD_MAX_RESPONSE_BYTES`, по умолчанию 4 MiB);
  * в логи попадают только HTTP-метод, путь (БЕЗ query) и статус: ни тело
    конверта (там СНИЛС/ФИО/СЭМД), ни Authorization, ни токены.

Ошибки домена РЭМД (`400/409` с конвертом `{status:"error", errors:[…]}`)
поднимаются как `RemdRejected` с кодом НСИ-каталога; транспортные — как
`Problem(502, "EMIAS_UNAVAILABLE")`.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request

from app.errors import Problem

from .auth import TokenCache
from .config import RemdConfig

log = logging.getLogger(__name__)

_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 8.0
_TOKEN_PATH = "/oauth/token"
_REGISTER_PATH = "/remd/v1/register-document/"
_SCOPE = "remd.write"
_REGISTRY_PATH = "/remd/v1/registry/{emdr_id}"


class RemdRejected(Exception):
    """РЭМД отверг запрос: код из каталога 1.2.643.5.1.13.13.99.2.305."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект = потенциальный уход на чужой хост → отказ (белый список)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class RemdClient:
    def __init__(
        self,
        cfg: RemdConfig | None = None,
        *,
        clock=time.monotonic,
        sleep=time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = cfg or RemdConfig.from_settings()
        self.cfg.validate()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._tokens = TokenCache(clock)
        self._opener = urllib.request.build_opener(_NoRedirect)

    # --- низкий уровень ------------------------------------------------------

    def _url(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    def _check_url(self, url: str) -> None:
        if not self.cfg.host_allowed(url):
            raise Problem(502, "EMIAS_UNAVAILABLE", "хост вне белого списка CC_REMD_BASE_URL")

    def _read(self, fp) -> bytes:
        data = fp.read(self.cfg.max_response_bytes + 1)
        if len(data) > self.cfg.max_response_bytes:
            raise Problem(502, "EMIAS_UNAVAILABLE", "тело ответа РЭМД превышает лимит")
        return data

    def _backoff(self, attempt: int) -> float:
        base = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
        return base + self._rng.uniform(0, base / 4)

    def _retry_after(self, headers) -> float | None:
        raw = headers.get("Retry-After") if headers else None
        if not raw:
            return None
        try:
            # Потолок обязателен: ожидание идёт внутри submit, `Retry-After: 86400`
            # иначе замораживает поток и транзакцию на сутки.
            return min(max(0.0, float(raw.strip())), self.cfg.retry_after_cap)
        except ValueError:
            return None  # HTTP-date игнорируем: сервер обязан отдать секунды

    def _send(self, method: str, url: str, *, headers: dict, data: bytes | None = None):
        """Один HTTP-обмен. Возвращает (status, headers, body_bytes)."""
        self._check_url(url)
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=self.cfg.timeout) as response:
                return response.status, response.headers, self._read(response)
        except urllib.error.HTTPError as exc:
            # Тело ошибки нужно для каталога: читаем с тем же капом.
            body = self._read(exc)
            return exc.code, exc.headers, body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(str(exc)) from exc

    def _call(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        form: dict | None = None,
        auth: bool = True,
        basic: bool = False,
    ) -> tuple[int, dict]:
        """Запрос с ретраями 429/5xx/таймаут и одним refresh на 401."""
        url = self._url(path)
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        attempts = self.cfg.max_attempts
        for attempt in range(1, attempts + 1):
            if auth:
                headers["Authorization"] = f"Bearer {self._access_token()}"
            elif basic:
                headers["Authorization"] = self._basic_credentials()
            try:
                status, response_headers, body = self._send(
                    method, url, headers=dict(headers), data=data
                )
            except ConnectionError:
                # Логируем только метод и путь: ПДн в URL не бывает, но всё равно.
                log.warning("REMd transport error: %s %s", method, path)
                if attempt == attempts:
                    raise Problem(
                        502, "EMIAS_UNAVAILABLE", "РЭМД недоступен (таймаут/сеть)"
                    ) from None
                self._sleep(self._backoff(attempt))
                continue

            if status == 401 and auth:
                # Токен мог протухнуть раньше заявленного — один refresh.
                if attempt < attempts:
                    self._tokens.clear()
                    continue
                raise Problem(502, "EMIAS_UNAVAILABLE", "РЭМД отклонил токен (401)")
            if status in (429, 500, 502, 503, 504):
                log.info("REMd retryable status %s: %s %s", status, method, path)
                if attempt == attempts:
                    raise Problem(
                        502, "EMIAS_UNAVAILABLE", f"РЭМД недоступен (HTTP {status})"
                    )
                delay = self._retry_after(response_headers)
                self._sleep(delay if delay is not None else self._backoff(attempt))
                continue

            parsed = self._json(body)
            if status >= 400:
                raise _rejected(status, parsed)
            return status, parsed

        raise Problem(502, "EMIAS_UNAVAILABLE", "РЭМД недоступен")  # pragma: no cover

    @staticmethod
    def _json(body: bytes) -> dict:
        if not body:
            return {}
        try:
            document = json.loads(body)
        except ValueError:
            raise Problem(502, "EMIAS_UNAVAILABLE", "РЭМД вернул не JSON") from None
        if not isinstance(document, dict):
            raise Problem(502, "EMIAS_UNAVAILABLE", "РЭМД вернул не объект JSON")
        return document

    # --- OAuth ---------------------------------------------------------------

    def _basic_credentials(self) -> str:
        raw = f"{self.cfg.client_id}:{self.cfg.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _access_token(self) -> str:
        token = self._tokens.get()
        if token is not None and not token.stale(self._clock()):
            return token.access_token
        _, body = self._call(
            "POST",
            _TOKEN_PATH,
            form={"grant_type": "client_credentials", "scope": _SCOPE},
            auth=False,
            basic=True,
        )
        access_token = body.get("access_token")
        if not access_token:
            raise Problem(502, "EMIAS_UNAVAILABLE", "РЭМД не вернул access_token")
        expires_in = body.get("expires_in")
        try:
            ttl = float(expires_in)
        except (TypeError, ValueError):
            ttl = 60.0
        scopes = str(body.get("scope") or "").split()
        self._tokens.put(str(access_token), ttl, scopes)
        return str(access_token)

    # --- контракт ------------------------------------------------------------

    def health(self) -> dict:
        """Проверка доступности: OAuth-обмен без отправки документа."""
        self._access_token()
        return {"authenticated": True}

    def register_document(self, envelope: dict) -> str:
        """Зарегистрировать СЭМД. Возвращает `request_id` (не emdr_id)."""
        _, body = self._call("POST", _REGISTER_PATH, payload=envelope)
        request_id = body.get("request_id")
        if not request_id:
            raise Problem(502, "EMIAS_UNAVAILABLE", "РЭМД не вернул request_id")
        return str(request_id)

    def registry_item(self, emdr_id: str) -> dict:
        """Элемент реестра (мок-only polling): status/errors/created_at."""
        _, body = self._call(
            "GET", _REGISTRY_PATH.format(emdr_id=urllib.parse.quote(emdr_id, safe=""))
        )
        return body


def _rejected(status: int, body: dict) -> RemdRejected:
    errors = body.get("errors") or []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    code = str(first.get("code") or "VALIDATION_ERROR")
    message = str(first.get("message") or "РЭМД отклонил запрос")
    return RemdRejected(code, message[:400], status=status)
