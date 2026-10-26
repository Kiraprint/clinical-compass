"""Общие стенды тестов FR-41: подписанный конверт, HTTP-мок и приёмник callback.

Мок РЭМД — отдельное ASGI-приложение (`app.remd.mock:app`), поэтому в тестах он
поднимается настоящим uvicorn в фоновом потоке: клиент ходит по HTTP, а не через
`TestClient`, и проверяется реальный протокол (OAuth, 202 + callback).

Callback-приёмник — маленький ThreadingHTTPServer: он фиксирует POST мока и
позволяет ответить ошибкой (проверка at-least-once/ретраев), а тест затем
передаёт тело в API-эндпоинт `/api/v1/remd/callback`.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import uvicorn

from app.signing import ca, cms

CDA_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<ClinicalDocument xmlns="urn:hl7-org:v3">'
    "<id root=\"1.2.643.5.1.13.13.12.2.77.1.1.9.1\" extension=\"{document_id}\"/>"
    "<title>Протокол приёма врача-психиатра</title>"
    "<effectiveTime value=\"20261026120000+0300\"/>"
    "</ClinicalDocument>"
)
SNILS = "112-233-445 95"


def make_cda(document_id: str = "doc-1") -> bytes:
    return CDA_TEMPLATE.format(document_id=document_id).encode()


def sign_cda(cda: bytes, fio: str = "Иванов Иван Иванович") -> bytes:
    """Отсоединённая CMS подписью демо-врача (тот же УЦ, что у подписи API)."""
    return cms.sign(cda, ca.issue_clinician(fio))


def envelope(
    *,
    cda: bytes | None = None,
    sig: bytes | None = None,
    local_uid: str = "11111111-2222-3333-4444-555555555555",
    kind: int = 34,
    number: str = "CC-doc-1",
    snils: str = SNILS,
    callback_url: str = "",
    lpu_uid: str = "1.2.643.5.1.13.13.12.2.77.1.1",
) -> dict:
    import base64

    cda = cda if cda is not None else make_cda()
    sig = sig if sig is not None else sign_cda(cda)
    return {
        "lpu_uid": lpu_uid,
        "local_document_uid": local_uid,
        "document_number": number,
        "kind": kind,
        "department": {"local_oid": "1.2.643.5.1.13.13.12.2.77.1.1.1",
                       "name": "Психиатрическое отделение"},
        "patient": {"local_id": "p-1", "surname": "Иванов", "name": "Иван",
                    "patr_name": "Иванович", "birth_date": "1990-01-01",
                    "snils": snils},
        "personals": [{"local_id": "d-1", "surname": "Петров", "name": "Пётр",
                       "patr_name": "Петрович", "role": "clinician"}],
        "cda": base64.b64encode(cda).decode(),
        "sig": base64.b64encode(sig).decode(),
        "callback_url": callback_url,
    }


class _Server(uvicorn.Server):
    pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MockRemd:
    """Мок РЭМД, поднятый на 127.0.0.1:порт (uvicorn в фоновом потоке)."""

    def __init__(self) -> None:
        from app.remd import mock

        self.module = mock
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        mock.reset_state()
        self._server = _Server(
            uvicorn.Config(mock.app, host="127.0.0.1", port=self.port,
                           log_level="warning", lifespan="off")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> "MockRemd":
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self._server.started:
            raise RuntimeError("мок РЭМД не стартовал")
        return self

    def __exit__(self, *_exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)

    @property
    def registry(self) -> dict:
        return self.module._registry

    def token(self, client_id: str = "compass-lpu", secret: str = "remd-secret") -> str:
        import base64
        import urllib.request

        body = b"grant_type=client_credentials"
        credentials = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        request = urllib.request.Request(
            f"{self.url}/oauth/token", data=body, method="POST",
            headers={"Authorization": f"Basic {credentials}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())["access_token"]


class CallbackSink:
    """HTTP-приёмник callback'ов мока (с управляемым статусом ответа)."""

    def __init__(self, status: int = 200) -> None:
        self.calls: list[dict] = []
        self.status = status
        self.event = threading.Event()
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — имя задано BaseHTTPRequestHandler
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                sink.calls.append(
                    {"path": self.path, "headers": dict(self.headers),
                     "body": json.loads(body or b"{}")}
                )
                sink.event.set()
                self.send_response(sink.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"lpu_uid": "1", "status": "success"}')

            def log_message(self, *_args):  # noqa: D401 — тишина в выводе тестов
                return

        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/callback"
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "CallbackSink":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wait(self, timeout: float = 10.0) -> dict | None:
        if not self.event.wait(timeout):
            return None
        self.event.clear()
        return self.calls[-1]

    def wait_calls(self, count: int, timeout: float = 10.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(self.calls) < count:
            time.sleep(0.01)
        return self.calls
