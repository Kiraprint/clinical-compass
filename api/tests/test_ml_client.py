"""Клиент ML-сайдкара (docs/08 §4): контракт, повторы, отказы.

Сайдкар не нужен: локальный HTTP-сервер на 127.0.0.1:0 отдаёт ответы из
очереди и считает запросы. Проверяем поведение клиента, а не модели.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import settings
from app.services import ml_client


class _Stub:
    """Очередь (status, body) на запросы; запоминает заголовки и тела."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict, dict]] = []
        self.responses: list[tuple[int, dict]] = []
        self.lock = threading.Lock()


@pytest.fixture
def stub(monkeypatch):
    state = _Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — контракт http.server
            self._reply()

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            with state.lock:
                # http.server канонизирует имена заголовков (X-Cc-Ml-Token),
                # поэтому храним их в нижнем регистре.
                state.requests.append(
                    (self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                )
            self._reply()

        def _reply(self) -> None:
            with state.lock:
                status, payload = state.responses.pop(0) if state.responses else (200, {})
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args) -> None:  # noqa: A002 — подпись базового класса
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(settings, "ml_url", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(settings, "ml_token", "s3cret")
    monkeypatch.setattr(ml_client, "_RETRY_DELAY", 0.0)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()

@pytest.fixture(autouse=True)
def _reset_breaker(monkeypatch):
    """Предохранитель — состояние модуля: сбрасываем между тестами."""
    monkeypatch.setattr(ml_client, "_breaker_failures", 0)
    monkeypatch.setattr(ml_client, "_breaker_open_until", 0.0)
    monkeypatch.setattr(ml_client, "_RETRY_DELAY", 0.0)
    yield


def _dead_transport(monkeypatch):
    """urlopen, который всегда падает по сети, со счётчиком обращений."""
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise urllib.error.URLError("down")

    monkeypatch.setattr(ml_client.urllib.request, "urlopen", boom)
    monkeypatch.setattr(settings, "ml_url", "http://127.0.0.1:1")
    return calls


def test_breaker_opens_after_three_network_failures(monkeypatch):
    calls = _dead_transport(monkeypatch)
    for _ in range(3):
        with pytest.raises(ml_client.MlUnavailable):
            ml_client.mask("Иванов Пётр")
    assert calls["n"] == 6  # по два захода на вызов (один повтор)
    with pytest.raises(ml_client.MlUnavailable) as err:
        ml_client.mask("Иванов Пётр")
    assert calls["n"] == 6, "разомкнутая цепь не должна ходить в сеть"
    assert "пауза" in str(err.value)


def test_breaker_half_open_after_cooldown(monkeypatch):
    calls = _dead_transport(monkeypatch)
    monkeypatch.setattr(ml_client, "_breaker_failures", ml_client._FAILURE_LIMIT)
    monkeypatch.setattr(ml_client, "_breaker_open_until", time.monotonic() - 1)
    with pytest.raises(ml_client.MlUnavailable):
        ml_client.mask("x")
    assert calls["n"] == 2, "после паузы вызов снова идёт в сеть (полуоткрытое состояние)"


def test_success_resets_failure_counter(monkeypatch, stub):
    monkeypatch.setattr(ml_client, "_breaker_failures", ml_client._FAILURE_LIMIT - 1)
    stub.responses = [(200, {"text": "ok", "spans": [], "model": "hybrid"})]
    ml_client.mask("x")
    assert ml_client._breaker_failures == 0
    assert ml_client._breaker_open_until == 0.0


def test_client_4xx_does_not_open_breaker(stub):
    for _ in range(5):
        stub.responses = [(422, {"detail": "bad"})]
        with pytest.raises(ml_client.MlUnavailable):
            ml_client.mask("x")
    assert ml_client._breaker_failures == 0, "4xx — ошибка запроса, а не отказ сервиса"
    stub.responses = [(200, {"text": "ok", "spans": [], "model": "hybrid"})]
    assert ml_client.mask("x")["text"] == "ok"


def test_disabled_without_url(monkeypatch):
    monkeypatch.setattr(settings, "ml_url", "")
    assert ml_client.enabled() is False
    with pytest.raises(ml_client.MlUnavailable):
        ml_client.mask("текст")

def test_embed_sends_kind_profile_and_token(stub):
    stub.responses = [(200, {"profile": "qwen3-0.6b", "dense_size": 3, "vectors": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]})]
    vectors = ml_client.embed(["раз", "два"], kind="query", profile="qwen3-0.6b")
    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    path, headers, body = stub.requests[0]
    assert path == "/v1/embeddings"
    assert headers.get("x-cc-ml-token") == "s3cret"
    assert body == {"texts": ["раз", "два"], "kind": "query", "profile": "qwen3-0.6b"}

def test_embed_rejects_dimension_mismatch(stub):
    stub.responses = [(200, {"dense_size": 3, "vectors": [[1.0, 0.0]]})]
    with pytest.raises(ml_client.MlUnavailable):
        ml_client.embed(["раз"])

def test_embed_rejects_vector_count_mismatch(stub):
    stub.responses = [(200, {"dense_size": 2, "vectors": [[1.0, 0.0]]})]
    with pytest.raises(ml_client.MlUnavailable):
        ml_client.embed(["раз", "два"])

def test_rerank_scores_aligned_with_documents(stub):
    stub.responses = [(200, {"scores": [0.2, 0.9]})]
    assert ml_client.rerank("q", ["a", "b"], top_k=2) == [0.2, 0.9]
    assert stub.requests[0][2] == {"query": "q", "documents": ["a", "b"], "top_k": 2}

def test_mask_returns_spans(stub):
    stub.responses = [(200, {"text": "[ФИО], 50 мг", "spans": [{"category": "FIO", "start": 0, "end": 5}], "model": "hybrid"})]
    data = ml_client.mask("Иванов, 50 мг", names=["Иванов"])
    assert data["text"].startswith("[ФИО]")
    assert data["spans"][0]["category"] == "FIO"
    assert stub.requests[0][2]["names"] == ["Иванов"]

def test_server_error_is_retried_once_then_unavailable(stub):
    stub.responses = [(503, {"detail": "loading"}), (503, {"detail": "loading"})]
    with pytest.raises(ml_client.MlUnavailable):
        ml_client.mask("текст")
    assert len(stub.requests) == 2

def test_client_error_is_not_retried(stub):
    stub.responses = [(422, {"detail": "bad"})]
    with pytest.raises(ml_client.MlUnavailable):
        ml_client.mask("текст")
    assert len(stub.requests) == 1

def test_ping_reads_health(stub):
    stub.responses = [(200, {"status": "ok", "device": "cuda", "models": {"ner": "hybrid"}})]
    assert ml_client.ping()["device"] == "cuda"
