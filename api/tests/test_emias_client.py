"""FR-25: клиент ЕМИАС — кэш токена, ретраи, кап тела, белый список, логи без ПДн."""

from __future__ import annotations

import json
import random
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.emias.client import EmiasClient, EmiasError
from app.emias.config import EmiasConfig, EmiasConfigError

def _bundle(*resources) -> bytes:
    return json.dumps({
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
        "link": [],
    }, ensure_ascii=False).encode()

PATIENT = {"resourceType": "Patient", "id": "emias-1001"}

class _Stub:
    def __init__(self) -> None:
        self.plan: list[dict] = []
        self.requests: list[tuple[str, str, str]] = []
        self.lock = threading.Lock()

    def push(self, status: int = 200, body: bytes = b"{}", headers: dict | None = None) -> None:
        self.plan.append({"status": status, "body": body, "headers": headers or {}})

    def next_step(self) -> dict:
        with self.lock:
            return self.plan.pop(0) if self.plan else {"status": 200, "body": b"{}", "headers": {}}

    def record(self, method: str, path: str, query: str) -> None:
        with self.lock:
            self.requests.append((method, path, query))

class _Handler(BaseHTTPRequestHandler):
    stub: _Stub

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        self.stub.record(self.command, parsed.path, parsed.query)
        if self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
        step = self.stub.next_step()
        body = step["body"]
        self.send_response(step["status"])
        for name, value in step["headers"].items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # тишина в тестах
        pass

@pytest.fixture()
def stub_server():
    stub = _Stub()
    handler = type("Handler", (_Handler,), {"stub": stub})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", stub
    server.shutdown()
    thread.join(timeout=5)

class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

def _client(base_url: str, clock: Clock | None = None, sleeps: list | None = None, **overrides) -> EmiasClient:
    cfg = EmiasConfig(
        enabled=True,
        base_url=base_url,
        client_id="compass",
        client_secret="secret",
        allow_insecure=True,
        timeout=2.0,
        max_attempts=overrides.pop("max_attempts", 3),
        max_response_bytes=overrides.pop("max_response_bytes", 4 * 1024 * 1024),
        snils_pepper="test-pepper",
    )
    sleeps = sleeps if sleeps is not None else []
    return EmiasClient(cfg, clock=clock or Clock(), sleep=sleeps.append, rng=random.Random(0))

def _token_body(token: str = "tok-1", expires_in: int = 100) -> bytes:
    return json.dumps({"access_token": token, "expires_in": expires_in, "token_type": "Bearer"}).encode()

def test_token_cache_reuses_token_until_80_percent(stub_server):
    base_url, stub = stub_server
    clock = Clock()
    client = _client(base_url, clock)

    stub.push(200, _token_body())
    stub.push(200, _bundle(PATIENT))
    assert client.list_patients()[0]["id"] == "emias-1001"

    stub.push(200, _bundle(PATIENT))
    assert client.list_patients()[0]["id"] == "emias-1001"
    token_calls = [r for r in stub.requests if r[1] == "/oauth/token"]
    assert len(token_calls) == 1  # второй вызов взял токен из кэша

    clock.now = 79.0
    stub.push(200, _bundle(PATIENT))
    client.list_patients()
    assert len([r for r in stub.requests if r[1] == "/oauth/token"]) == 1

    clock.now = 81.0  # перешли 80% TTL
    stub.push(200, _token_body("tok-2"))
    stub.push(200, _bundle(PATIENT))
    client.list_patients()
    assert len([r for r in stub.requests if r[1] == "/oauth/token"]) == 2

def test_retry_on_429_honours_retry_after(stub_server):
    base_url, stub = stub_server
    sleeps: list[float] = []
    client = _client(base_url, sleeps=sleeps)
    stub.push(200, _token_body())
    stub.push(429, b"{}", {"Retry-After": "0.5"})
    stub.push(200, _bundle(PATIENT))

    assert client.list_patients()[0]["id"] == "emias-1001"
    assert sleeps == [0.5]

def test_retry_on_500_then_success(stub_server):
    base_url, stub = stub_server
    sleeps: list[float] = []
    client = _client(base_url, sleeps=sleeps)
    stub.push(200, _token_body())
    stub.push(500, b"{}")
    stub.push(200, _bundle(PATIENT))

    assert client.list_patients()[0]["id"] == "emias-1001"
    assert len(sleeps) == 1 and sleeps[0] > 0

def test_gives_up_after_max_attempts(stub_server):
    base_url, stub = stub_server
    sleeps: list[float] = []
    client = _client(base_url, sleeps=sleeps, max_attempts=3)
    stub.push(200, _token_body())
    for _ in range(3):
        stub.push(503, b"{}")

    with pytest.raises(EmiasError) as exc:
        client.list_patients()
    assert exc.value.code == "server_error"
    assert exc.value.retryable is True
    assert len(sleeps) == 2  # между тремя попытками

def test_response_size_cap(stub_server):
    base_url, stub = stub_server
    client = _client(base_url, max_response_bytes=200)
    stub.push(200, _token_body())
    big = json.dumps({"resourceType": "Bundle", "type": "searchset", "total": 0,
                      "entry": [], "padding": "x" * 500}).encode()
    stub.push(200, big)
    with pytest.raises(EmiasError) as exc:
        client.list_patients()
    assert exc.value.code == "response_too_large"

def test_rejects_non_json_bundle(stub_server):
    base_url, stub = stub_server
    client = _client(base_url)
    stub.push(200, _token_body())
    stub.push(200, b"<html>not json</html>")
    with pytest.raises(EmiasError) as exc:
        client.list_patients()
    assert exc.value.code == "invalid_json"

def test_rejects_wrong_resource_type(stub_server):
    base_url, stub = stub_server
    client = _client(base_url)
    stub.push(200, _token_body())
    stub.push(200, _bundle({"resourceType": "Observation", "id": "o1"}))
    with pytest.raises(EmiasError) as exc:
        client.list_patients()
    assert exc.value.code == "invalid_bundle"

def test_redirect_is_refused(stub_server):
    base_url, stub = stub_server
    client = _client(base_url)
    stub.push(200, _token_body())
    stub.push(302, b"", {"Location": "http://evil.example/steal"})
    with pytest.raises(EmiasError) as exc:
        client.list_patients()
    assert exc.value.code == "redirect_refused"

def test_host_allowlist_rejects_foreign_host():
    client = _client("http://127.0.0.1:8099")
    with pytest.raises(EmiasError) as exc:
        client._check_url("https://emias.example/fhir/Patient")
    assert exc.value.code == "host_not_allowed"

def test_config_requires_https_unless_insecure_and_forbids_userinfo():
    with pytest.raises(EmiasConfigError):
        EmiasConfig(enabled=True, base_url="http://emias.example", client_id="a",
                    client_secret="b").validate()
    with pytest.raises(EmiasConfigError):
        EmiasConfig(enabled=True, base_url="https://user:pass@emias.example", client_id="a",
                    client_secret="b").validate()
    with pytest.raises(EmiasConfigError):
        EmiasConfig(enabled=True, base_url="https://emias.example", client_id="",
                    client_secret="").validate()

def test_no_phi_or_token_in_logs(stub_server):
    """Логи содержат только метод/путь/статус: ни СНИЛС, ни токена.

    caplog в этом окружении ненадёжен (логгер app.* глушится на коллекции —
    см. tests/test_depersonalize_ner.py), поэтому перехватываем своим handler'ом.
    """
    import logging

    base_url, stub = stub_server
    client = _client(base_url)
    stub.push(200, _token_body("secret-token-value"))
    stub.push(500, b"{}")
    stub.push(200, _bundle(PATIENT))

    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    logger = logging.getLogger("app.emias.client")
    handler = _Capture()
    was_disabled = logger.disabled
    logger.disabled = False
    logger.addHandler(handler)
    try:
        client.search_patients("11223344595")
    finally:
        logger.removeHandler(handler)
        logger.disabled = was_disabled

    text = "\n".join(messages)
    assert text, "клиент обязан логировать факт ретрая"
    assert "11223344595" not in text
    assert "112-233-445" not in text
    assert "secret-token-value" not in text
    assert "identifier=" not in text  # query (со СНИЛС) не логируется
    assert "/fhir/Patient" in text

def test_resource_ids_are_validated():
    """P3: id ЕМИАС — только [A-Za-z0-9._-], иначе path traversal в путь запроса."""
    from app.emias.client import EmiasError, EmiasClient

    assert EmiasClient._safe_id("emias-1001") == "emias-1001"
    for bad in ("../admin", "a/b", "a b", "x" * 65, "", "a?b=1"):
        with pytest.raises(EmiasError) as exc:
            EmiasClient._safe_id(bad)
        assert exc.value.code == "invalid_id"

def test_demo_secret_rejected_outside_mock():
    """P2-2: демо-секрет мока допустим только с allow_insecure (контур мока)."""
    from app.emias.config import EmiasConfig, EmiasConfigError

    with pytest.raises(EmiasConfigError):
        EmiasConfig(
            enabled=True, base_url="https://emias.example", client_id="compass",
            client_secret="compass-secret",
        ).validate()
    # внутри мока (http + allow_insecure) — допустимо
    EmiasConfig(
        enabled=True, base_url="http://emias-mock:8090", client_id="compass",
        client_secret="compass-secret", allow_insecure=True,
    ).validate()

def test_client_secret_not_in_repr():
    from app.emias.config import EmiasConfig

    cfg = EmiasConfig(enabled=True, base_url="https://e.example", client_id="c", client_secret="top-secret")
    assert "top-secret" not in repr(cfg)
