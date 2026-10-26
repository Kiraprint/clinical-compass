"""Контракт /v1/transcribe (docs/18 §4.3) в stub-режиме: без GPU и torch.

Проверяем форму ответа, коды ошибок и гарантии по сегментам; настоящий
backend (faster-whisper) в CI не грузится — для него нужен GPU.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import create_app
from models import Registry

# 32 000 байт/с — оценка stub'а (16 кГц, mono, int16).
_BYTES_PER_S = 32_000
# Stub-сегмент — 5 секунд (models._ASR_STUB_SEGMENT_S).
_SEGMENT_S = 5.0


def _detail(response) -> dict:
    body = response.json()
    assert set(body) == {"detail"}, body
    assert set(body["detail"]) == {"code", "message"}, body
    return body["detail"]


def _audio(seconds: float = 12.0) -> bytes:
    return b"\x00" * int(seconds * _BYTES_PER_S)


def _two_mib() -> bytes:
    return b"\x00" * (2 * 1024 * 1024)


def _app(monkeypatch, *, asr: bool = True, load: bool = True):
    """Приложение поверх stub-реестра с (по умолчанию) загруженным ASR."""
    monkeypatch.setenv("CC_ML_ASR", "1" if asr else "0")
    registry = Registry(stub=True)
    registry.warmup()  # модели поиска готовы; ASR грузится лениво
    if load:
        registry.load_asr()
    return create_app(registry, start_warmup=False), registry


def _post(
    client: TestClient,
    *,
    data: bytes | None = None,
    content_type: str = "audio/webm",
    **form,
):
    return client.post(
        "/v1/transcribe",
        files={"file": ("session.webm", _audio() if data is None else data, content_type)},
        data=form,
    )


# --- форма ответа ---------------------------------------------------------

def test_transcribe_stub_shape(monkeypatch):
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "model",
        "device",
        "duration_s",
        "rtf",
        "language",
        "segments",
        "speakers",
        "degraded",
    }
    assert body["model"] == "stub"
    assert body["device"] == "stub"
    assert body["language"] == "ru"
    assert body["duration_s"] == pytest.approx(12.0, abs=0.01)
    # RTF < 1 у любой реальной модели; у stub он практически нулевой.
    assert 0 <= body["rtf"] < 1
    assert body["speakers"] == []
    # stub — полноценный ответ, а не деградация: degraded true только от diarize.
    assert body["degraded"] is False
    assert body["segments"]
    for segment in body["segments"]:
        assert set(segment) == {"start", "end", "speaker", "text"}
        assert segment["speaker"] is None
        assert segment["text"].startswith("[stub]")
        assert segment["start"] < segment["end"]


def test_transcribe_language_override(monkeypatch):
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, language="en")
    assert response.status_code == 200, response.text
    assert response.json()["language"] == "en"


def test_transcribe_bad_language(monkeypatch):
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, language="ru-RU")
    assert response.status_code == 400
    assert _detail(response)["code"] == "invalid_request"


def test_transcribe_codecs_suffix_accepted(monkeypatch):
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, content_type="audio/webm;codecs=opus")
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("seconds", [0.25, 5.0, 12.0, 47.3])
def test_segments_monotonic(monkeypatch, seconds):
    """start < end, без пересечений, по возрастанию, в пределах duration."""
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, data=_audio(seconds))
    assert response.status_code == 200, response.text
    body = response.json()
    segments = body["segments"]
    assert segments
    previous_end = 0.0
    for segment in segments:
        assert segment["start"] < segment["end"]
        assert segment["start"] >= previous_end - 1e-6, segments
        assert segment["end"] <= body["duration_s"] + 1e-6
        previous_end = segment["end"]
    # stub: длительность из размера файла, один сегмент на 5 секунд.
    expected = max(1, -(-int(seconds * _BYTES_PER_S) // int(_SEGMENT_S * _BYTES_PER_S)))
    assert len(segments) == expected


def test_diarize_degrades_honestly(monkeypatch):
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, diarize="true")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["degraded"] is True
    assert body["speakers"] == []
    assert all(segment["speaker"] is None for segment in body["segments"])


# --- ошибки ---------------------------------------------------------------

def test_transcribe_401_without_token(monkeypatch):
    app, registry = _app(monkeypatch)
    monkeypatch.setenv("CC_ML_TOKEN", "s3cret")
    with TestClient(create_app(registry, start_warmup=False)) as client:
        response = _post(client)
    assert response.status_code == 401
    assert _detail(response)["code"] == "unauthorized"


def test_transcribe_413_oversize_endpoint(monkeypatch):
    """Точный размер режет эндпоинт: middleware-лимит посчитан раньше."""
    app, _ = _app(monkeypatch)
    monkeypatch.setenv("CC_ML_ASR_MAX_MB", "1")
    with TestClient(app) as client:
        response = _post(client, data=_two_mib())  # 2 МиБ при потолке 1 МБ
    assert response.status_code == 413
    assert _detail(response)["code"] == "payload_too_large"


def test_transcribe_413_oversize_middleware(monkeypatch):
    """Content-Length больше потолка отсекается до разбора мультипарта."""
    monkeypatch.setenv("CC_ML_ASR_MAX_MB", "1")
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, data=_two_mib())
    assert response.status_code == 413
    assert _detail(response)["code"] == "payload_too_large"


def test_transcribe_415_wrong_type(monkeypatch):
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, content_type="application/json")
    assert response.status_code == 415
    assert _detail(response)["code"] == "unsupported_media"


def test_transcribe_400_empty_file(monkeypatch):
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, data=b"")
    assert response.status_code == 400
    assert _detail(response)["code"] == "invalid_request"


def test_transcribe_503_asr_disabled(monkeypatch):
    app, _ = _app(monkeypatch, asr=False)
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 503
    assert _detail(response)["code"] == "asr_disabled"


def test_transcribe_503_while_loading(monkeypatch):
    """Веса ещё грузятся: 503 model_loading + Retry-After, как у остальных."""
    monkeypatch.setenv("CC_ML_ASR", "1")
    registry = Registry(stub=True)
    registry._status = "ready"  # noqa: SLF001 — модели поиска уже готовы
    with TestClient(create_app(registry, start_warmup=False)) as client:
        response = _post(client)
        assert response.status_code == 503, response.text
        assert _detail(response)["code"] == "model_loading"
        assert response.headers.get("Retry-After") == "10"
        registry.load_asr()  # дочитываем, чтобы поток не висел после теста


def test_health_reports_asr_state(monkeypatch):
    monkeypatch.setenv("CC_ML_ASR", "1")
    registry = Registry(stub=True)
    registry._status = "ready"  # noqa: SLF001 — без фоновой загрузки ASR
    with TestClient(create_app(registry, start_warmup=False)) as client:
        before = client.get("/health").json()["models"]["asr"]
        assert before == {
            "enabled": True,
            "status": "idle",
            "model": None,
        }
        registry.load_asr()
        after = client.get("/health").json()["models"]["asr"]
        assert after["status"] == "ready"
        assert after["model"] == "dropbox-dash/faster-whisper-large-v3-turbo"
