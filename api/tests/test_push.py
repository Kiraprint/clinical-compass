"""VAPID-ключ: демо-ключ из compose обязан быть валидным EC P-256.

Регресс: в infra/docker-compose.yml лежала строка, которая base64-разбиралась,
но не была корректным PKCS8 DER, из-за чего /push/vapid-public-key отвечал
422 «Invalid key», а воркер напоминаний не мог подписать push.
"""

import base64
import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import settings
from app.services import push

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
