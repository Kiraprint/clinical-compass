"""Мок ЕМИАС (FR-25): самостоятельное FastAPI-приложение для офлайн-проверки.

Запуск:  uvicorn app.emias.mock:app --host 127.0.0.1 --port 8090
В compose: сервис `emias-mock` (профиль emias, infra/docker-compose.yml).

Контракт повторяет реальный REST/FHIR-гибрид ЕМИАС в объёме, нужном клиенту:
  POST /oauth/token                          client_credentials (Basic), 401/429
  GET  /fhir/Patient?identifier=&_count=&_offset=   Bundle(searchset) + total + link[next]
  GET  /fhir/Patient/{id}
  GET  /fhir/Encounter?patient=&date=ge…     история посещений
  GET  /fhir/MedicationRequest?patient=      история назначений
Права: scope токена (patient.read / encounter.read / medication.read) → 403.
Инъекция сбоев: ?simulate=500|429|timeout; задержка — CC_EMIAS_MOCK_LATENCY_MS,
таймаут-сон — CC_EMIAS_MOCK_TIMEOUT_S, лимит токенов — CC_EMIAS_MOCK_RATE_LIMIT/мин.

Данные фикстуры детерминированы (3 пациента), ПДн вымышленные. Мок НЕ
воспроизводит mTLS/ГОСТ-TLS, СМЭВ, справочники НСИ и ЭЦП — см. docs/09 §7.
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
import threading
import time
from collections import deque
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

SNILS_SYSTEM = "urn:oid:1.2.643.100.3"

DEFAULT_CLIENT_ID = "compass"
DEFAULT_CLIENT_SECRET = "compass-secret"
TOKEN_TTL = 3600
SCOPE_BY_RESOURCE = {
    "Patient": "patient.read",
    "Encounter": "encounter.read",
    "MedicationRequest": "medication.read",
}

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default

# --- фикстуры ----------------------------------------------------------------

PATIENTS: list[dict] = [
    {
        "resourceType": "Patient",
        "id": "emias-1001",
        "identifier": [
            {"system": SNILS_SYSTEM, "type": {"text": "SNILS"}, "value": "112-233-445 95"},
            {"system": "urn:oid:1.2.643.5.1.13.13.99.2.3", "value": "0000000000001001"},
        ],
        "name": [{"use": "official", "family": "Иванова", "given": ["Мария", "Петровна"]}],
        "telecom": [{"system": "phone", "value": "+79990000001", "use": "mobile"}],
        "birthDate": "1985-04-12",
        "address": [{"city": "Москва", "line": ["ул. Тверская, д. 1"]}],
    },
    {
        "resourceType": "Patient",
        "id": "emias-1002",
        "identifier": [
            {"system": SNILS_SYSTEM, "type": {"text": "SNILS"}, "value": "112-233-446 96"},
        ],
        "name": [{"use": "official", "family": "Петров", "given": ["Алексей", "Игоревич"]}],
        "telecom": [{"system": "phone", "value": "+79990000002", "use": "mobile"}],
        "birthDate": "1979-11-30",
        "address": [{"city": "Москва", "line": ["ул. Арбат, д. 12"]}],
    },
    {
        "resourceType": "Patient",
        "id": "emias-1003",
        "identifier": [
            {"system": SNILS_SYSTEM, "type": {"text": "SNILS"}, "value": "112-233-447 97"},
        ],
        "name": [{"use": "official", "family": "Сидорова", "given": ["Ольга", "Викторовна"]}],
        "telecom": [{"system": "phone", "value": "+79990000003", "use": "mobile"}],
        "birthDate": "1992-07-05",
        "address": [{"city": "Москва", "line": ["Ленинский пр-т, д. 55"]}],
    },
]

def _encounter(enc_id: str, patient_id: str, start: str, end: str, klass: str, reason: str, doctor: str) -> dict:
    return {
        "resourceType": "Encounter",
        "id": enc_id,
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": klass},
        "period": {"start": start, "end": end},
        "serviceProvider": {"display": "ГБУЗ «Городская поликлиника № 1»"},
        "reasonCode": [{"text": reason}],
        "participant": [{"individual": {"display": doctor}}],
        "subject": {"reference": f"Patient/{patient_id}"},
    }

ENCOUNTERS: dict[str, list[dict]] = {
    "emias-1001": [
        _encounter("enc-1001-1", "emias-1001", "2025-11-04T09:00:00+03:00", "2025-11-04T09:25:00+03:00",
                   "AMB", "Плановый приём терапевта", "Терапевт Ковалёва Н.С."),
        _encounter("enc-1001-2", "emias-1001", "2026-01-19T14:10:00+03:00", "2026-01-19T14:40:00+03:00",
                   "AMB", "Контрольный приём, тревожные жалобы", "Терапевт Ковалёва Н.С."),
        _encounter("enc-1001-3", "emias-1001", "2026-03-02T10:00:00+03:00", "2026-03-02T10:30:00+03:00",
                   "VR", "Телемедицинская консультация", "Психотерапевт Белов Д.А."),
    ],
    "emias-1002": [
        _encounter("enc-1002-1", "emias-1002", "2025-12-11T08:30:00+03:00", "2025-12-11T09:00:00+03:00",
                   "AMB", "Диспансерный осмотр", "Терапевт Гринёв П.П."),
        _encounter("enc-1002-2", "emias-1002", "2026-02-27T16:00:00+03:00", "2026-02-27T16:20:00+03:00",
                   "AMB", "Жалобы на нарушение сна", "Терапевт Гринёв П.П."),
    ],
    "emias-1003": [
        _encounter("enc-1003-1", "emias-1003", "2026-01-08T11:15:00+03:00", "2026-01-08T11:45:00+03:00",
                   "AMB", "Первичный приём", "Терапевт Орлова Е.В."),
        _encounter("enc-1003-2", "emias-1003", "2026-04-16T09:40:00+03:00", "2026-04-16T10:05:00+03:00",
                   "AMB", "Повторный приём", "Терапевт Орлова Е.В."),
    ],
}

def _medication(med_id: str, title: str, dosage: str, authored: str, status: str, patient_id: str) -> dict:
    return {
        "resourceType": "MedicationRequest",
        "id": med_id,
        "status": status,
        "intent": "order",
        "medicationCodeableConcept": {"text": title},
        "dosageInstruction": [{"text": dosage}],
        "authoredOn": authored,
        "subject": {"reference": f"Patient/{patient_id}"},
    }

MEDICATIONS: dict[str, list[dict]] = {
    "emias-1001": [
        _medication("med-1001-1", "Сертралин", "50 мг 1 раз в сутки, вечером", "2026-01-19", "active", "emias-1001"),
        _medication("med-1001-2", "Гидроксизин", "25 мг на ночь по потребности", "2025-11-04", "completed", "emias-1001"),
    ],
    "emias-1002": [
        _medication("med-1002-1", "Миртазапин", "15 мг на ночь", "2026-02-27", "active", "emias-1002"),
        _medication("med-1002-2", "Мелатонин", "3 мг за 30 минут до сна", "2025-12-11", "stopped", "emias-1002"),
    ],
    "emias-1003": [
        _medication("med-1003-1", "Эсциталопрам", "10 мг утром", "2026-01-08", "active", "emias-1003"),
        _medication("med-1003-2", "Магний B6", "1 таблетка 2 раза в день", "2026-01-08", "completed", "emias-1003"),
    ],
}

# --- состояние (в памяти процесса; мок одноразовый) --------------------------

_tokens: dict[str, dict] = {}
_token_attempts: dict[str, deque] = {}
_lock = threading.Lock()

app = FastAPI(title="EMIAS mock", version="0.1.0")

def _outcome(status: int, code: str, diagnostics: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": code, "diagnostics": diagnostics}],
        },
    )

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
    client_id, secret = raw.split(":", 1)
    return client_id, secret

def _check_rate(client_id: str) -> bool:
    limit = _env_int("CC_EMIAS_MOCK_RATE_LIMIT", 60)
    window = 60.0
    now = time.monotonic()
    with _lock:
        bucket = _token_attempts.setdefault(client_id, deque())
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

def _simulate(request: Request) -> JSONResponse | None:
    """Инъекция сбоев: ?simulate=500|429|timeout (docs/06 §3)."""
    mode = request.query_params.get("simulate")
    if not mode:
        return None
    if mode == "500":
        return _outcome(500, "exception", "simulated internal error")
    if mode == "429":
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "1"},
            content={"resourceType": "OperationOutcome",
                     "issue": [{"severity": "error", "code": "throttled"}]},
        )
    if mode == "timeout":
        time.sleep(max(0.0, _env_float("CC_EMIAS_MOCK_TIMEOUT_S", 2.0)))
        return _outcome(504, "timeout", "simulated gateway timeout")
    return None

def _latency() -> None:
    delay_ms = _env_int("CC_EMIAS_MOCK_LATENCY_MS", 0)
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)

def _bearer(request: Request) -> dict | JSONResponse:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return _outcome(401, "login", "требуется Bearer-токен")
    token = header[7:]
    with _lock:
        record = _tokens.get(token)
    if record is None or record["expires_at"] < time.time():
        return _outcome(401, "login", "токен недействителен")
    return record

def _require_scope(record: dict, resource_type: str) -> JSONResponse | None:
    needed = SCOPE_BY_RESOURCE[resource_type]
    if needed not in record["scopes"]:
        return JSONResponse(
            status_code=403,
            content={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "forbidden",
                           "diagnostics": f"insufficient_scope: {needed}"}],
            },
        )
    return None

def _bundle(request: Request, path: str, resources: list[dict], limit: int, offset: int) -> dict:
    page = resources[offset: offset + limit]
    base = str(request.base_url).rstrip("/")
    entries = []
    for resource in page:
        entries.append({
            "fullUrl": f"{base}{path}/{resource['id']}",
            "resource": resource,
        })
    links = [{"relation": "self", "url": f"{base}{path}?{urlencode({**request.query_params, '_count': limit, '_offset': offset})}"}]
    if offset + limit < len(resources):
        links.append({
            "relation": "next",
            "url": f"{base}{path}?{urlencode({**request.query_params, '_count': limit, '_offset': offset + limit})}",
        })
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "link": links,
        "entry": entries,
    }

def _paging(request: Request) -> tuple[int, int]:
    try:
        limit = int(request.query_params.get("_count", "10"))
    except ValueError:
        limit = 10
    try:
        offset = int(request.query_params.get("_offset", "0"))
    except ValueError:
        offset = 0
    return max(1, min(limit, 100)), max(0, offset)

# --- endpoints ---------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "emias-mock"}

@app.post("/oauth/token")
async def oauth_token(request: Request):
    simulated = _simulate(request)
    if simulated is not None:
        return simulated
    delay_ms = _env_int("CC_EMIAS_MOCK_LATENCY_MS", 0)
    if delay_ms:
        time.sleep(delay_ms / 1000.0)

    creds = _basic_credentials(request)
    if creds is None:
        return JSONResponse(
            status_code=401,
            headers={"WWW-Authenticate": "Basic"},
            content={"error": "invalid_client"},
        )
    client_id, secret = creds
    expected_id = os.environ.get("CC_EMIAS_CLIENT_ID", DEFAULT_CLIENT_ID)
    expected_secret = os.environ.get("CC_EMIAS_CLIENT_SECRET", DEFAULT_CLIENT_SECRET)
    if not (hmac.compare_digest(client_id, expected_id) and hmac.compare_digest(secret, expected_secret)):
        return JSONResponse(status_code=401, content={"error": "invalid_client"})
    if not _check_rate(client_id):
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "1"},
            content={"error": "temporarily_unavailable"},
        )

    try:
        body = await request.body()
        form = dict(parse_qsl(body.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        form = {}
    if form.get("grant_type") != "client_credentials":
        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})

    requested = form.get("scope", "").split()
    scopes = requested or list(SCOPE_BY_RESOURCE.values())
    unknown = [s for s in scopes if s not in SCOPE_BY_RESOURCE.values()]
    if unknown:
        return JSONResponse(status_code=400, content={"error": "invalid_scope"})

    token = f"mock-{secrets.token_hex(16)}"
    with _lock:
        _tokens[token] = {"client_id": client_id, "scopes": scopes,
                          "expires_at": time.time() + TOKEN_TTL}
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL,
        "scope": " ".join(scopes),
    }

@app.get("/fhir/Patient")
def search_patient(request: Request):
    simulated = _simulate(request)
    if simulated is not None:
        return simulated
    _latency()
    record = _bearer(request)
    if isinstance(record, JSONResponse):
        return record
    denied = _require_scope(record, "Patient")
    if denied is not None:
        return denied
    limit, offset = _paging(request)
    identifier = request.query_params.get("identifier")
    resources = PATIENTS
    if identifier:
        normalized = "".join(ch for ch in identifier if ch.isdigit())
        resources = [
            p for p in PATIENTS
            if any("".join(ch for ch in (i.get("value") or "") if ch.isdigit()) == normalized
                   for i in p.get("identifier", []))
        ]
    return _bundle(request, "/fhir/Patient", resources, limit, offset)

@app.get("/fhir/Patient/{patient_id}")
def get_patient(patient_id: str, request: Request):
    simulated = _simulate(request)
    if simulated is not None:
        return simulated
    _latency()
    record = _bearer(request)
    if isinstance(record, JSONResponse):
        return record
    denied = _require_scope(record, "Patient")
    if denied is not None:
        return denied
    for patient in PATIENTS:
        if patient["id"] == patient_id:
            return patient
    return _outcome(404, "not-found", "пациент не найден")

@app.get("/fhir/Encounter")
def search_encounters(request: Request):
    simulated = _simulate(request)
    if simulated is not None:
        return simulated
    _latency()
    record = _bearer(request)
    if isinstance(record, JSONResponse):
        return record
    denied = _require_scope(record, "Encounter")
    if denied is not None:
        return denied
    patient_id = request.query_params.get("patient", "")
    if not patient_id:
        return _outcome(400, "required", "параметр patient обязателен")
    limit, offset = _paging(request)
    resources = ENCOUNTERS.get(patient_id, [])
    date_filter = request.query_params.get("date", "")
    if date_filter.startswith("ge"):
        threshold = date_filter[2:]
        resources = [e for e in resources if (e["period"].get("start") or "") >= threshold]
    return _bundle(request, "/fhir/Encounter", resources, limit, offset)

@app.get("/fhir/MedicationRequest")
def search_medications(request: Request):
    simulated = _simulate(request)
    if simulated is not None:
        return simulated
    _latency()
    record = _bearer(request)
    if isinstance(record, JSONResponse):
        return record
    denied = _require_scope(record, "MedicationRequest")
    if denied is not None:
        return denied
    patient_id = request.query_params.get("patient", "")
    if not patient_id:
        return _outcome(400, "required", "параметр patient обязателен")
    limit, offset = _paging(request)
    return _bundle(request, "/fhir/MedicationRequest", MEDICATIONS.get(patient_id, []), limit, offset)

def reset_state() -> None:
    """Для тестов: чистое состояние токенов/лимитов."""
    with _lock:
        _tokens.clear()
        _token_attempts.clear()
