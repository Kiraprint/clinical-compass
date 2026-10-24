"""VAPID: демо-ключ из compose валиден, а доставка реально подписывается.

Регрессы: (1) в infra/docker-compose.yml лежал base64, который не был корректным
PKCS8 DER → /push/vapid-public-key отдавал 422 «Invalid key», воркер не мог
подписать push; (2) pywebpush трактует строку vapid_private_key как путь к файлу,
поэтому PEM-текст не подписывал ничего — нужен объект Vapid.
"""

import base64
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import settings
from app.models import PushSubscription
from app.services import push

from .conftest import clinician_ctx, run_async

COMPOSE = Path(__file__).resolve().parents[2] / "infra" / "docker-compose.yml"
_KEY_RE = re.compile(r'CC_VAPID_PRIVATE_KEY: "\$\{CC_VAPID_PRIVATE_KEY:-([^}]+)\}"')


def _compose_default_key() -> str:
    match = _KEY_RE.search(COMPOSE.read_text(encoding="utf-8"))
    assert match, "в compose нет CC_VAPID_PRIVATE_KEY с дефолтом"
    return match.group(1)


def _load(key_b64: str) -> ec.EllipticCurvePrivateKey:
    pem = base64.b64decode(key_b64)
    key = serialization.load_pem_private_key(pem, password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    return key


def test_compose_vapid_key_is_valid_p256():
    key = _load(_compose_default_key())
    assert key.curve.name == "secp256r1"


@pytest.fixture
def vapid_env(monkeypatch):
    monkeypatch.setattr(push, "_vapid_cache", None)
    yield monkeypatch
    push._vapid_cache = None


def test_vapid_public_key_matches_private(vapid_env):
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    vapid_env.setattr(settings, "vapid_private_key", base64.b64encode(pem).decode())

    public = push.vapid_public_key()
    assert len(public) == 87 and public.startswith("B")
    raw = base64.urlsafe_b64decode(public + "=")
    assert raw[0] == 4  # несжатая точка X9.62



def test_invalid_vapid_key_raises(vapid_env):
    vapid_env.setattr(settings, "vapid_private_key", base64.b64encode(b"not a key").decode())

    with pytest.raises(ValueError):
        push.vapid_public_key()


class _Collector(BaseHTTPRequestHandler):
    """Push-сервис браузера на loopback: складывает запросы, отвечает 201."""

    received: list[tuple[str, dict[str, str], bytes]] = []

    def do_POST(self):  # noqa: N802 — имя задано http.server
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        headers = {k.lower(): v for k, v in self.headers.items()}
        type(self).received.append((self.path, headers, body))
        self.send_response(201)
        self.end_headers()

    def log_message(self, *args):  # тишина в выводе тестов
        pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_push_to_clinicians_signs_request(client, db_session, monkeypatch):
    """Доставка врачу реально подписана VAPID-ключом из настроек."""
    _Collector.received = []
    srv = HTTPServer(("127.0.0.1", 0), _Collector)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _, clinician_id = clinician_ctx(client, "pushsign@test.ru")
        browser = ec.generate_private_key(ec.SECP256R1())
        p256dh = browser.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        db_session.add(
            PushSubscription(
                user_id=clinician_id,
                endpoint=f"http://127.0.0.1:{srv.server_port}/push/{uuid.uuid4().hex}",
                p256dh=_b64url(p256dh),
                auth=_b64url(b"\x01" * 16),
            )
        )
        db_session.commit()

        monkeypatch.setattr(settings, "push_enabled", True)
        monkeypatch.setattr(push, "_vapid_cache", None)
        run_async(push.push_to_clinicians, [uuid.UUID(clinician_id)], "тест", "тело")
        db_session.expire_all()
    finally:
        srv.shutdown()
        push._vapid_cache = None

    assert len(_Collector.received) == 1, "push не ушёл — подпись не собралась"
    _, headers, body = _Collector.received[0]
    # http.server канонизирует имена заголовков: сравниваем в нижнем регистре.
    assert headers["authorization"].startswith("vapid t=")
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["ttl"] == "0"
    assert body != json.dumps({"title": "тест", "body": "тело"}).encode()  # зашифровано


def test_endpoint_host_hides_capability_token():
    """Endpoint подписки — capability-токен: в лог идёт только хост."""
    host = push._endpoint_host("https://fcm.googleapis.com/wp/AbC-sEcReT-token?x=1")
    assert host == "fcm.googleapis.com"
    assert "AbC-sEcReT" not in host
