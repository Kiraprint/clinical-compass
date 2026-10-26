"""Мок РЭМД/ЕГИСЗ (FR-41): самостоятельное FastAPI-приложение.

`uvicorn app.remd.mock:app --port 8091` — офлайн-проверка исходящей передачи
СЭМД без ЕГИСЗ (docs/18 §8, research §2–§5). Мок повторяет протокол обмена, а
не «принимает всё»:

  * OAuth `client_credentials` + Bearer на `/remd/v1/*`;
  * `registerDocument` → **202 + request_id**, результат приходит асинхронно на
    callback URL настоящим HTTP POST (`registerDocumentResult`), at-least-once;
  * отсоединённая CMS-подпись проверяется криптографически
    (`app.signing.cms.verify_cms` + цепочка до демо-УЦ + срок сертификата),
    а не «непустым base64»;
  * идемпотентность по `local_document_uid`: тот же UID и тот же sha256 → тот же
    `request_id` и ни одной новой записи реестра; другой контент → 409
    `NOT_UNIQUE_PROVIDED_ID`;
  * каталог ошибок 1.2.643.5.1.13.13.99.2.305 с реальными HTTP-кодами;
  * инъекция сбоев `?simulate=500|429|timeout` (как у мока ЕМИАС).

Состояние — в памяти процесса; `reset_state()` для тестов. СНИЛС/ФИО не
логируются и не печатаются.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from app.signing import ca, cms, gost

from .mapping import NSI_1520_KINDS, snils_valid

log = logging.getLogger(__name__)

TOKEN_TTL = 3600
DEFAULT_CLIENT_ID = "compass-lpu"
DEFAULT_CLIENT_SECRET = "remd-secret"
DEFAULT_LPU_UID = "1.2.643.5.1.13.13.12.2.77.1.1"
SUBMIT_SCOPE = "remd.write"
CALLBACK_HEADER = "X-Remd-Token"
CDA_ROOT = "{urn:hl7-org:v3}ClinicalDocument"

_lock = threading.Lock()
_tokens: dict[str, float] = {}
_scopes: dict[str, set[str]] = {}
_fail_first_left: int | None = None
_registry: dict[str, dict] = {}  # request_id → запись реестра
_documents: dict[str, dict] = {}  # local_document_uid → {sha256, request_id}
_document_numbers: dict[tuple, str] = {}  # (number, kind, snils) → local_document_uid
_callbacks: dict[str, dict] = {}  # request_id → последний отправленный callback
_requests: dict[str, dict] = {}  # request_id → get-metadata/search (не документы)
_register_calls = 0  # сколько раз вызывали registerDocument (ретраи клиента)


def reset_state() -> None:
    """Сброс состояния мока (тесты, smoke)."""
    global _fail_first_left, _register_calls
    with _lock:
        _fail_first_left = None
        _register_calls = 0
        _tokens.clear()
        _scopes.clear()
        _registry.clear()
        _documents.clear()
        _document_numbers.clear()
        _callbacks.clear()
        _requests.clear()

def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


app = FastAPI(title="REMD mock", version="0.1.0")


# --- ответы ------------------------------------------------------------------


def _error(status: int, code: str, message: str, request_id: str | None = None,
           headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        headers=headers,
        content={
            "request_id": request_id,
            "status": "error",
            "errors": [{"code": code, "message": message}],
        },
    )


def _accepted(request_id: str) -> JSONResponse:
    return JSONResponse(status_code=202, content={"request_id": request_id})


# --- аутентификация ----------------------------------------------------------


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return None
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in raw:
        return None
    return tuple(raw.split(":", 1))  # type: ignore[return-value]


def _bearer_ok(request: Request) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    with _lock:
        expires_at = _tokens.get(header[7:])
    return expires_at is not None and expires_at > time.monotonic()


def _scopes_of(request: Request) -> set[str]:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return set()
    with _lock:
        return set(_scopes.get(header[7:], set()))


def _forbidden_scope() -> JSONResponse:
    """403 по шаблону мока ЕМИАС: не каталог НСИ, а нехватка scope у токена."""
    return _error(403, "INSUFFICIENT_SCOPE", "токену нужен scope remd.write")


def _unauthorized() -> JSONResponse:
    return _error(401, "UNAUTHORIZED", "нужен действующий Bearer-токен")


# --- инъекция сбоев ----------------------------------------------------------


async def _simulate(request: Request, request_id: str | None = None) -> JSONResponse | None:
    """`?simulate=500|429|timeout` — как в моке ЕМИАС (docs/06 §3)."""
    mode = request.query_params.get("simulate")
    if not mode:
        return None
    if mode == "500":
        return _error(500, "INTERNAL_ERROR", "simulated internal error", request_id)
    if mode == "429":
        return _error(429, "RATE_LIMITED", "simulated quota", request_id,
                      headers={"Retry-After": "1"})
    if mode == "timeout":
        # Не блокируем loop мока: иначе «таймаут» одного запроса останавливает
        # и обработку callback'ов.
        await asyncio.sleep(max(0.0, _env_float("CC_REMD_MOCK_TIMEOUT_S", 2.0)))
        return _error(504, "INTERNAL_ERROR", "simulated timeout", request_id)
    return None


def _forced_fault() -> JSONResponse | None:
    """`CC_REMD_MOCK_FORCE_STATUS=500|429` — постоянный сбой для тестов ретраев."""
    status = _env("CC_REMD_MOCK_FORCE_STATUS", "")
    if status not in {"429", "500", "503"}:
        return None
    code = {"429": "RATE_LIMITED", "500": "INTERNAL_ERROR", "503": "MIS_NOT_AVAILABLE"}[status]
    headers = {"Retry-After": _env("CC_REMD_MOCK_RETRY_AFTER", "0")} if status == "429" else None
    return _error(int(status), code, "forced fault", headers=headers)


def _fail_first() -> JSONResponse | None:
    """`CC_REMD_MOCK_FAIL_FIRST=N`: первые N регистраций — 500."""
    global _fail_first_left
    with _lock:
        if _fail_first_left is None:
            _fail_first_left = max(0, _env_int("CC_REMD_MOCK_FAIL_FIRST", 0))
        if _fail_first_left > 0:
            _fail_first_left -= 1
            return _error(500, "INTERNAL_ERROR", "injected fault")
    return None


# --- проверка конверта -------------------------------------------------------


def _decode_base64(value, field: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field)
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(field)


def _verify_signature(cda: bytes, signature_der: bytes) -> JSONResponse | None:
    """Криптографическая проверка отсоединённой CMS над байтами СЭМД."""
    ok, info, reason = cms.verify_cms(cda, signature_der)
    if not ok or info is None:
        return _error(400, "INVALID_SIGNATURE", f"подпись не сходится ({reason})")
    if info.certificate.expired():
        return _error(400, "CERTIFICATE_EXPIRED", "сертификат подписанта вне срока действия")
    try:
        authority = ca.authority().der
    except Exception:  # noqa: BLE001 — УЦ недоступен: это сбой мока, не документа
        log.exception("REMD mock: demo CA unavailable")
        return _error(500, "INTERNAL_ERROR", "демо-УЦ недоступен в контуре мока")
    if not cms.chain_ok(info.certificate, authority):
        return _error(400, "CERTIFICATE_NOT_FOUND", "сертификат не выпущен доверенным УЦ")
    return None


def _xml_ok(cda: bytes) -> bool:
    try:
        root = ET.fromstring(cda)
    except ET.ParseError:
        return False
    return root.tag == CDA_ROOT


# --- callback ----------------------------------------------------------------


def _callback_payload(entry: dict, message_type: str) -> dict:
    base = {
        "from_system_id": "remd-mock",
        "lpu_uid": entry["lpu_uid"],
        "response_to_request_id": entry["request_id"],
        "message_type": message_type,
    }
    if message_type == "getMetadataResult":
        return {
            **base,
            "status": "success",
            "data": {
                "emdr_id": entry["emdr_id"],
                "status": entry["status"],
                "created_at": entry["created_at"],
            },
        }
    if message_type == "searchRegistryItemResult":
        return {**base, "status": "success", "data": {"items": entry["items"]}}
    return {
        **base,
        "status": "success",
        "data": {
            "emdr_id": entry["emdr_id"],
            "registration_date": entry["created_at"],
            "retention_period": "25y",
            "status": "REGISTERED",
        },
    }


def _client_success(body: bytes) -> bool:
    """2xx считаем успехом доставки только при явном `{"status":"success"}`."""
    try:
        answer = json.loads(body or b"{}")
    except ValueError:
        return False
    return isinstance(answer, dict) and answer.get("status") == "success"


def _deliver(request_id: str) -> bool:
    """Доставить последний callback: настоящий HTTP POST, ретраи с backoff."""
    with _lock:
        entry = _registry.get(request_id) or _requests.get(request_id)
        payload = _callbacks.get(request_id)
    if entry is None or payload is None:
        return False
    before = entry.get("status")
    url = _env("CC_REMD_MOCK_CALLBACK_URL", "") or entry.get("callback_url") or ""
    if not url:
        with _lock:
            entry["callback_error"] = "callback_url_missing"
        return False
    attempts = max(1, _env_int("CC_REMD_MOCK_CALLBACK_ATTEMPTS", 5))
    base_delay = max(0.0, _env_float("CC_REMD_MOCK_CALLBACK_BACKOFF_S", 0.2))
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    token = _env("CC_REMD_MOCK_CALLBACK_TOKEN", "")
    if token:
        headers[CALLBACK_HEADER] = token

    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                answer = response.read()
                accepted = response.status < 300 and _client_success(answer)
                if accepted:
                    with _lock:
                        if payload.get("message_type") == "registerDocumentResult":
                            entry["status"] = "REGISTERED"
                        entry["updated_at"] = _now()
                        entry.pop("callback_error", None)
                    return True
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            with _lock:
                entry["callback_error"] = f"attempt {attempt}: {exc.__class__.__name__}"
        if attempt < attempts and base_delay:
            time.sleep(base_delay * attempt)
    with _lock:
        # Терминальный REGISTERED повторной доставкой не понижается.
        entry["status"] = before or "VALIDATING"
        entry["updated_at"] = _now()
    return False


def _queue(request_id: str, tasks: BackgroundTasks) -> None:
    tasks.add_task(_deliver, request_id)


# --- endpoints ---------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/oauth/token")
async def oauth_token(request: Request):
    credentials = _basic_credentials(request)
    expected = (_env("CC_REMD_MOCK_CLIENT_ID", DEFAULT_CLIENT_ID),
                _env("CC_REMD_MOCK_CLIENT_SECRET", DEFAULT_CLIENT_SECRET))
    if credentials != expected:
        return _error(401, "UNAUTHORIZED", "неверные client_id/client_secret")
    grant = ""
    scope = ""
    content_type = request.headers.get("Content-Type", "")
    if "application/json" in content_type:
        try:
            form = await request.json() or {}
        except ValueError:
            form = {}
        grant, scope = form.get("grant_type", ""), form.get("scope", "")
    else:
        # application/x-www-form-urlencoded разбираем stdlib-ом: мок не тянет
        # python-multipart (он нужен только для загрузки файлов).
        raw = (await request.body()).decode("utf-8", "replace")
        fields = urllib.parse.parse_qs(raw)
        grant = (fields.get("grant_type") or [""])[0]
        scope = (fields.get("scope") or [""])[0]
    if grant != "client_credentials":
        return _error(400, "VALIDATION_ERROR", "grant_type=client_credentials")
    # Без явного scope токен полноправный (совместимость с простыми клиентами);
    # с явным — только перечисленное, и submit требует remd.write.
    scopes = set(scope.split()) or {SUBMIT_SCOPE}
    token = uuid.uuid4().hex
    with _lock:
        _tokens[token] = time.monotonic() + TOKEN_TTL
        _scopes[token] = scopes
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL,
        "scope": " ".join(sorted(scopes)),
    }


@app.post("/remd/v1/register-document/")
async def register_document(request: Request, tasks: BackgroundTasks):
    global _register_calls
    if not _bearer_ok(request):
        return _unauthorized()
    if SUBMIT_SCOPE not in _scopes_of(request):
        return _forbidden_scope()
    with _lock:
        _register_calls += 1
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "VALIDATION_ERROR", "тело должно быть JSON")
    if not isinstance(body, dict):
        return _error(400, "VALIDATION_ERROR", "тело должно быть объектом")
    request_id = body.get("request_id")

    simulated = await _simulate(request, request_id) or _forced_fault() or _fail_first()
    if simulated is not None:
        return simulated

    lpu_uid = str(body.get("lpu_uid") or "")
    if lpu_uid != _env("CC_REMD_MOCK_LPU_UID", DEFAULT_LPU_UID):
        return _error(400, "ORGANIZATION_NOT_FOUND", "lpu_uid не зарегистрирован в моке",
                      request_id)
    local_uid = str(body.get("local_document_uid") or "")
    try:
        uuid.UUID(local_uid)
    except ValueError:
        return _error(400, "VALIDATION_ERROR", "local_document_uid должен быть UUID",
                      request_id)
    kind = body.get("kind")
    if kind not in NSI_1520_KINDS:
        return _error(400, "UNKNOWN_DOCUMENT_KIND", "код вида СЭМД вне НСИ 1520", request_id)
    patient = body.get("patient") or {}
    snils = str(patient.get("snils") or "")
    if not snils_valid(snils):
        return _error(400, "INVALID_SNILS", "СНИЛС не проходит контрольную сумму", request_id)
    number = str(body.get("document_number") or "").strip()
    if not number:
        return _error(400, "VALIDATION_ERROR", "document_number обязателен", request_id)
    department = body.get("department") or {}
    if not isinstance(department, dict) or not department.get("local_oid"):
        return _error(400, "VALIDATION_ERROR", "department.local_oid обязателен", request_id)
    if not body.get("personals"):
        return _error(400, "VALIDATION_ERROR", "personals не пуст", request_id)

    try:
        cda = _decode_base64(body.get("cda"), "cda")
        signature_der = _decode_base64(body.get("sig"), "sig")
    except ValueError as exc:
        return _error(400, "VALIDATION_ERROR", f"поле {exc} должно быть base64", request_id)
    if not _xml_ok(cda):
        return _error(400, "VALIDATION_ERROR", "cda не является CDA R2 ClinicalDocument",
                      request_id)
    signature_error = _verify_signature(cda, signature_der)
    if signature_error is not None:
        return signature_error

    sha256 = gost.digest(cda).hex()
    with _lock:
        known = _documents.get(local_uid)
        if known is not None:
            if known["sha256"] == sha256:
                # Идемпотентность: та же версия документа — тот же request_id,
                # новых записей реестра и повторного callback нет.
                return _accepted(known["request_id"])
            return _error(409, "NOT_UNIQUE_PROVIDED_ID",
                          "local_document_uid уже принят с другим содержимым", request_id)
        number_key = (number, int(kind), snils)
        if number_key in _document_numbers:
            return _error(409, "DUPLICATE_DOCUMENT",
                          "документ с таким номером уже зарегистрирован", request_id)
        if request_id and request_id in _registry:
            return _accepted(str(request_id))
        request_id = str(request_id or uuid.uuid4())
        emdr_id = str(uuid.uuid4())
        entry = {
            "request_id": request_id,
            "emdr_id": emdr_id,
            "lpu_uid": lpu_uid,
            "local_document_uid": local_uid,
            "kind": int(kind),
            "status": "VALIDATING",
            "errors": [],
            "created_at": _now(),
            "updated_at": _now(),
            "snils": snils,
            "callback_url": str(body.get("callback_url") or ""),
        }
        _registry[request_id] = entry
        _documents[local_uid] = {"sha256": sha256, "request_id": request_id}
        _document_numbers[number_key] = local_uid
        _callbacks[request_id] = _callback_payload(entry, "registerDocumentResult")
    _queue(request_id, tasks)
    log.info("REMD mock: accepted document, request %s", request_id)
    return _accepted(request_id)


@app.post("/remd/v1/get-metadata/")
async def get_metadata(request: Request, tasks: BackgroundTasks):
    if not _bearer_ok(request):
        return _unauthorized()
    body = await request.json()
    request_id = body.get("request_id")
    simulated = await _simulate(request, request_id) or _forced_fault()
    if simulated is not None:
        return simulated
    emdr_id = str(body.get("emdr_id") or "")
    with _lock:
        entry = next((e for e in _registry.values() if e["emdr_id"] == emdr_id), None)
        if entry is None:
            return _error(400, "VALIDATION_ERROR", "emdr_id не найден", request_id)
        reply = dict(entry)
        reply["request_id"] = str(request_id or uuid.uuid4())
        _callbacks[reply["request_id"]] = _callback_payload(reply, "getMetadataResult")
        _requests[reply["request_id"]] = reply
    _queue(reply["request_id"], tasks)
    return _accepted(reply["request_id"])


@app.post("/remd/v1/search-registry-item/")
async def search_registry_item(request: Request, tasks: BackgroundTasks):
    if not _bearer_ok(request):
        return _unauthorized()
    body = await request.json()
    request_id = body.get("request_id")
    simulated = await _simulate(request, request_id) or _forced_fault()
    if simulated is not None:
        return simulated
    snils = str((body.get("patient") or {}).get("snils") or "")
    if not snils_valid(snils):
        return _error(400, "INVALID_SNILS", "СНИЛС не проходит контрольную сумму", request_id)
    kind = body.get("kind")
    with _lock:
        items = [
            {
                "emdr_id": entry["emdr_id"],
                "status": entry["status"],
                "created_at": entry["created_at"],
            }
            for entry in _registry.values()
            if entry.get("snils") == snils and (kind is None or entry["kind"] == kind)
        ]
        reply_id = str(request_id or uuid.uuid4())
        reply = {
            "request_id": reply_id,
            "emdr_id": "",
            "lpu_uid": _env("CC_REMD_MOCK_LPU_UID", DEFAULT_LPU_UID),
            "status": "SEARCH",
            "created_at": _now(),
            "updated_at": _now(),
            "items": items,
        }
        _requests[reply_id] = reply
        _callbacks[reply_id] = _callback_payload(reply, "searchRegistryItemResult")
    _queue(reply_id, tasks)
    return _accepted(reply_id)


@app.get("/remd/v1/registry/{emdr_id}")
async def registry_item(emdr_id: str):
    """Mock-only polling: состояние записи реестра без callback."""
    with _lock:
        entry = next((e for e in _registry.values() if e["emdr_id"] == emdr_id), None)
    if entry is None:
        return _error(404, "VALIDATION_ERROR", "emdr_id не найден")
    return {
        "emdr_id": entry["emdr_id"],
        "status": entry["status"],
        "errors": entry.get("errors") or [],
        "created_at": entry["created_at"],
        "updated_at": entry["updated_at"],
    }


@app.post("/remd/v1/_test/callback-retry/{request_id}")
async def callback_retry(request_id: str, request: Request):
    """Тестовый хук: повторная доставка последнего callback (at-least-once)."""
    if not _bearer_ok(request):
        return _unauthorized()
    with _lock:
        known = request_id in _callbacks
    if not known:
        return _error(404, "VALIDATION_ERROR", "callback не найден", request_id)
    delivered = await asyncio.to_thread(_deliver, request_id)
    return {"request_id": request_id, "delivered": delivered}
