"""HTTP-клиент ЕМИАС (FR-25). Только stdlib: urllib/json/base64/hmac.

Что клиент гарантирует (docs/10 R17-R19):
  * белый список хостов: каждый запрос сверяется с netloc CC_EMIAS_BASE_URL,
    redirect на другой хост запрещён (urllib не следует редиректам);
  * OAuth client_credentials, токен кэшируется и обновляется на 80% TTL,
    повторный 401 → один принудительный refresh;
  * до 3 попыток на 429/5xx/таймаут с экспоненциальным backoff и jitter,
    Retry-After соблюдается;
  * жёсткий кап тела ответа (CC_EMIAS_MAX_RESPONSE_BYTES, по умолчанию 4 MiB);
  * в логи попадают только HTTP-метод, путь (БЕЗ query — там СНИЛС) и статус;
    тела ответов, заголовок Authorization и токены не логируются никогда.

FHIR-валидация: ресурс Bundle (searchset), entry ≤ 1000, тип ресурсов совпадает.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .auth import TokenCache
from .config import SCOPES, EmiasConfig

log = logging.getLogger(__name__)

MAX_BUNDLE_ENTRIES = 1000
MAX_PAGES = 10
_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 8.0

class EmiasError(Exception):
    """Ошибка интеграции. code — машиночитаемый, PHI в сообщениях нет."""

    def __init__(self, code: str, message: str = "", *, status: int | None = None, retryable: bool = False):
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.retryable = retryable

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект = потенциальный уход на чужой хост → отказ (белый список)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None

class EmiasClient:
    def __init__(
        self,
        cfg: EmiasConfig | None = None,
        *,
        clock=time.monotonic,
        sleep=time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = cfg or EmiasConfig.from_settings()
        self.cfg.validate()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._tokens = TokenCache(clock)
        self._opener = urllib.request.build_opener(_NoRedirect)

    # --- низкий уровень ------------------------------------------------------

    def _url(self, path: str, params: dict | None = None) -> str:
        url = self.cfg.base_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _check_url(self, url: str) -> None:
        if not self.cfg.host_allowed(url):
            raise EmiasError("host_not_allowed", "хост вне белого списка CC_EMIAS_BASE_URL")

    def _read(self, fp) -> bytes:
        data = fp.read(self.cfg.max_response_bytes + 1)
        if len(data) > self.cfg.max_response_bytes:
            raise EmiasError("response_too_large", "тело ответа превышает лимит")
        return data

    def _backoff(self, attempt: int) -> float:
        base = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
        return base + self._rng.uniform(0, base / 4)

    @staticmethod
    def _retry_after(headers) -> float | None:
        raw = headers.get("Retry-After") if headers else None
        if not raw:
            return None
        try:
            return max(0.0, float(raw.strip()))
        except ValueError:
            return None  # HTTP-date игнорируем: сервер обязан отдать секунды

    def _send(self, method: str, url: str, *, headers: dict, data: bytes | None = None):
        """Один HTTP-обмен. Возвращает (status, headers, body_bytes).

        429/5xx ретраятся здесь; сетевые ошибки/таймауты тоже.
        """
        self._check_url(url)
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self._opener.open(req, timeout=self.cfg.timeout) as resp:
                    return resp.status, resp.headers, self._read(resp)
            except urllib.error.HTTPError as err:
                status = err.code
                body = self._read(err)
                retry_after = self._retry_after(err.headers)
                err.close()
                if status == 429 or status >= 500:
                    if attempt >= self.cfg.max_attempts:
                        raise EmiasError(
                            "rate_limited" if status == 429 else "server_error",
                            f"ЕМИАС вернул {status}",
                            status=status,
                            retryable=True,
                        ) from None
                    delay = retry_after if retry_after is not None else self._backoff(attempt)
                    log.warning("emias: %s %s → %s, retry %s/%s", method, urllib.parse.urlparse(url).path,
                                status, attempt, self.cfg.max_attempts)
                    self._sleep(delay)
                    continue
                if 300 <= status < 400:
                    raise EmiasError("redirect_refused", "redirect на другой хост запрещён", status=status)
                if status in (401, 403):
                    raise EmiasError(
                        "unauthorized" if status == 401 else "insufficient_scope",
                        f"ЕМИАС отказал ({status})",
                        status=status,
                    )
                if status == 404:
                    raise EmiasError("not_found", "ресурс не найден в ЕМИАС", status=status)
                raise EmiasError("http_error", f"ЕМИАС вернул {status}", status=status)
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                # URLError несёт таймаут/отказ соединения; тело не читаем.
                if attempt >= self.cfg.max_attempts:
                    raise EmiasError("network_error", "нет связи с ЕМИАС", retryable=True) from None
                log.warning("emias: %s %s: сеть (%s), retry %s/%s", method,
                            urllib.parse.urlparse(url).path, type(err).__name__,
                            attempt, self.cfg.max_attempts)
                self._sleep(self._backoff(attempt))

    def _request_json(self, method: str, path: str, params: dict | None = None, *, auth: bool = True) -> dict:
        url = self._url(path, params)
        if auth:
            token = self._access_token()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            try:
                status, _hdrs, body = self._send(method, url, headers=headers)
            except EmiasError as err:
                if err.code != "unauthorized":
                    raise
                # Токен мог быть отозван раньше TTL → один принудительный refresh.
                self._tokens.clear()
                headers = {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}
                status, _hdrs, body = self._send(method, url, headers=headers)
        else:
            status, _hdrs, body = self._send(
                method, url, headers={"Accept": "application/json"}
            )
        if status != 200:
            raise EmiasError("http_error", f"ЕМИАС вернул {status}", status=status)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise EmiasError("invalid_json", "ответ ЕМИАС не JSON") from None
        if not isinstance(payload, dict):
            raise EmiasError("invalid_json", "ответ ЕМИАС — не объект")
        return payload

    # --- OAuth ---------------------------------------------------------------

    def _fetch_token(self) -> None:
        basic = base64.b64encode(
            f"{self.cfg.client_id}:{self.cfg.client_secret}".encode("utf-8")
        ).decode("ascii")
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": " ".join(SCOPES)}
        ).encode("ascii")
        payload = self._request_json_raw_post(
            "/oauth/token",
            body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not token:
            raise EmiasError("invalid_token", "в ответе нет access_token")
        try:
            ttl = float(expires_in)
        except (TypeError, ValueError):
            raise EmiasError("invalid_token", "некорректный expires_in") from None
        scope = payload.get("scope") or ""
        self._tokens.put(token, ttl, scope.split() if isinstance(scope, str) else ())

    def _request_json_raw_post(self, path: str, body: bytes, *, headers: dict) -> dict:
        url = self._url(path)
        status, _hdrs, raw = self._send("POST", url, headers={**headers, "Accept": "application/json"}, data=body)
        if status != 200:
            raise EmiasError("unauthorized", f"ЕМИАС отказал ({status})", status=status)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise EmiasError("invalid_json", "ответ ЕМИАС не JSON") from None
        if not isinstance(payload, dict):
            raise EmiasError("invalid_json", "ответ ЕМИАС — не объект")
        return payload

    def _access_token(self) -> str:
        cached = self._tokens.get()
        if cached is not None and not cached.stale(self._clock()):
            return cached.access_token
        self._fetch_token()
        return self._tokens.get().access_token

    # --- FHIR ----------------------------------------------------------------

    @staticmethod
    def validate_bundle(payload: dict, resource_type: str) -> list[dict]:
        if payload.get("resourceType") != "Bundle":
            raise EmiasError("invalid_bundle", "resourceType != Bundle")
        if payload.get("type") != "searchset":
            raise EmiasError("invalid_bundle", "type != searchset")
        entries = payload.get("entry", [])
        if not isinstance(entries, list) or len(entries) > MAX_BUNDLE_ENTRIES:
            raise EmiasError("invalid_bundle", "entry отсутствует или превышает 1000")
        resources: list[dict] = []
        for item in entries:
            resource = item.get("resource") if isinstance(item, dict) else None
            if not isinstance(resource, dict) or resource.get("resourceType") != resource_type:
                raise EmiasError("invalid_bundle", "некорректный entry.resource")
            resources.append(resource)
        return resources

    def _search(self, path: str, params: dict, resource_type: str) -> dict:
        payload = self._request_json("GET", path, params)
        resources = self.validate_bundle(payload, resource_type)
        return {"payload": payload, "resources": resources}

    def list_patients(self, *, count: int = 100) -> list[dict]:
        """Все пациенты, доступные контуру (страницы по _count, не более 10)."""
        return self._collect("/fhir/Patient", {}, "Patient", count)

    def search_patients(self, snils: str) -> list[dict]:
        return self._collect("/fhir/Patient", {"identifier": snils}, "Patient", 100)

    @staticmethod
    def _safe_id(value: str) -> str:
        """Идентификатор ЕМИАС — только [A-Za-z0-9._-]: защита от path traversal
        и подмешивания query/fragment в путь (P3 ревью)."""
        if not _ID_RE.fullmatch(value or ""):
            raise EmiasError("invalid_id", "некорректный идентификатор ресурса ЕМИАС")
        return urllib.parse.quote(value, safe="")

    def get_patient(self, emias_patient_id: str) -> dict:
        payload = self._request_json("GET", f"/fhir/Patient/{self._safe_id(emias_patient_id)}")
        if payload.get("resourceType") != "Patient":
            raise EmiasError("invalid_resource", "ожидался Patient")
        return payload

    def encounters(self, emias_patient_id: str, *, since: str | None = None) -> list[dict]:
        params = {"patient": self._safe_id(emias_patient_id)}
        if since:
            params["date"] = f"ge{since}"
        return self._collect("/fhir/Encounter", params, "Encounter", 100)

    def medications(self, emias_patient_id: str) -> list[dict]:
        return self._collect(
            "/fhir/MedicationRequest", {"patient": self._safe_id(emias_patient_id)}, "MedicationRequest", 100
        )

    def _collect(self, path: str, params: dict, resource_type: str, count: int) -> list[dict]:
        collected: list[dict] = []
        offset = 0
        for _page in range(MAX_PAGES):
            page_params = {**params, "_count": count, "_offset": offset}
            payload = self._request_json("GET", path, page_params)
            batch = self.validate_bundle(payload, resource_type)
            collected.extend(batch)
            total = payload.get("total")
            offset += len(batch)
            if not batch or not isinstance(total, int) or offset >= total:
                break
        return collected
