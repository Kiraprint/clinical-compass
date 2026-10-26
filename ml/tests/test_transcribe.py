"""Контракт /v1/transcribe (docs/18 §4.3) в stub-режиме: без GPU и torch.

Проверяем форму ответа, коды ошибок и гарантии по сегментам; настоящий
backend (faster-whisper) в CI не грузится — для него нужен GPU.
"""

from __future__ import annotations

import io
import threading
import time

import pytest
from fastapi.testclient import TestClient

import models
from app import create_app
from models import Registry, StubTranscriber

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
        "diarized",
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


@pytest.mark.parametrize("language", ["ru-RU", "rus", "xx", "russian"])
def test_transcribe_bad_language(monkeypatch, language):
    """Код языка проверяется до бэкенда: иначе Whisper падает 500'ой."""
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, language=language)
    assert response.status_code == 400
    detail = _detail(response)
    assert detail["code"] == "invalid_request"
    assert language in detail["message"]


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


def test_diarize_stub_assigns_speakers(monkeypatch):
    """Stub-диаризация: реплики получают A/B, метки и суммарные секунды."""
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, diarize="true")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["diarized"] is True
    assert body["degraded"] is False
    labels = [segment["speaker"] for segment in body["segments"]]
    assert labels, body
    assert set(labels) == {"A", "B"}
    # Stub чередует спикеров каждые 5 с: 12 с = три сегмента A, B, A.
    assert labels == ["A", "B", "A"]
    speakers = {item["id"]: item for item in body["speakers"]}
    assert set(speakers) == {"A", "B"}
    for item in body["speakers"]:
        assert item["label"] is None  # роли назначает api, не сайдкар
        assert item["segments"] > 0
        assert item["seconds"] > 0
    # Сумма секунд по спикерам равна сумме длительностей размеченных сегментов.
    total = sum(segment["end"] - segment["start"] for segment in body["segments"])
    assert sum(item["seconds"] for item in body["speakers"]) == pytest.approx(total, abs=0.01)


def test_diarize_disabled_is_not_degraded(monkeypatch):
    """CC_ML_DIARIZE=0: спикеров нет, но это решение конфига, а не сбой."""
    monkeypatch.setenv("CC_ML_DIARIZE", "0")
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, diarize="true")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["diarized"] is False
    assert body["degraded"] is False
    assert body["speakers"] == []
    assert all(segment["speaker"] is None for segment in body["segments"])


def test_diarize_failure_degrades_honestly(monkeypatch):
    """Упавшая диаризация — 200 с diarized=false, а не 422/500."""

    class Broken:
        model_id = "broken"
        device = "stub"

        def diarize(self, audio, duration_s):
            raise RuntimeError("модель развалилась")

    monkeypatch.setattr(models, "StubDiarizer", Broken)
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, diarize="true")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["diarized"] is False
    assert body["degraded"] is True
    assert body["speakers"] == []
    assert all(segment["speaker"] is None for segment in body["segments"])


def test_diarize_missing_weights_degrades(monkeypatch):
    """Нет весов (каталог пуст) — реестр деградирует, статус остаётся error."""
    app, registry = _app(monkeypatch)
    registry.stub = False  # как в проде: настоящий backend, но весов нет
    registry.diarize_model_dir = "/nonexistent/diarize"
    with TestClient(app) as client:
        response = _post(client, diarize="true")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["diarized"] is False
    assert body["degraded"] is True
    assert registry.status()["models"]["diarize"]["status"] == "error"


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
    # Выключенная фича повтором не лечится — Retry-After тут только путал бы.
    assert response.headers.get("Retry-After") is None


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


def test_transcribe_422_backend_failure(monkeypatch):
    """Сбой бэкенда на самом аудио — 422 с конвертом, а не 500 text/plain."""
    app, registry = _app(monkeypatch)

    class _Broken:
        model_id = "stub"
        device = "stub"

        def transcribe(self, data: bytes, language: str) -> dict:
            raise ValueError("не декодируется")

    registry._asr = _Broken()  # noqa: SLF001 — имитация сбоя декодера
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 422, response.text
    assert _detail(response)["code"] == "asr_failed"


def test_transcribe_503_does_not_wait_for_loading(monkeypatch):
    """Пока веса грузятся, ответ 503 отдаётся сразу, а не после загрузки."""
    monkeypatch.setenv("CC_ML_ASR", "1")
    registry = Registry(stub=True)
    registry._status = "ready"  # noqa: SLF001 — модели поиска уже готовы
    registry._asr_status = "loading"  # noqa: SLF001 — загрузчик держит замок
    registry._asr_lock.acquire()  # noqa: SLF001
    try:
        with TestClient(create_app(registry, start_warmup=False)) as client:
            started = time.perf_counter()
            response = _post(client)
            elapsed = time.perf_counter() - started
    finally:
        registry._asr_lock.release()  # noqa: SLF001
    assert response.status_code == 503, response.text
    assert _detail(response)["code"] == "model_loading"
    # Замок занят «загрузкой» — запрос обязан ответить, не дожидаясь её.
    assert elapsed < 1.0, elapsed

# --- контейнеры video/* ---------------------------------------------------
@pytest.mark.parametrize("media", ["video/webm", "video/mp4"])
def test_transcribe_video_container_accepted(monkeypatch, media):
    """MediaRecorder с видеодорожкой и прокси по расширению дают video/webm."""
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, content_type=media)
    assert response.status_code == 200, response.text


def test_transcribe_video_unsupported(monkeypatch):
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = _post(client, content_type="video/quicktime")
    assert response.status_code == 415
    assert _detail(response)["code"] == "unsupported_media"


# --- конверт для ошибок Starlette/python-multipart ------------------------
def test_multipart_without_boundary_uses_envelope(monkeypatch):
    """Starlette отвечает 400 сам: тело обязано быть конвертом, не строкой."""
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/v1/transcribe",
            content=b"\x00" * 32,
            headers={"Content-Type": "multipart/form-data"},
        )
    assert response.status_code == 400, response.text
    assert _detail(response)["code"] == "invalid_request"


def test_multipart_oversized_part_uses_envelope(monkeypatch):
    """Не-файловая часть > 1 МиБ: python-multipart рубит запрос 400'ой."""
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/v1/transcribe",
            files={"file": ("session.webm", _audio(1.0), "audio/webm")},
            data={"language": "ru" * 1_000_000},
        )
    assert response.status_code == 400, response.text
    assert _detail(response)["code"] == "invalid_request"


# --- кривой CC_ML_ASR_MAX_MB не роняет сайдкар ---------------------------
def test_bad_max_mb_does_not_break_disabled_asr(monkeypatch):
    monkeypatch.setenv("CC_ML_ASR_MAX_MB", "abc")
    app, _ = _app(monkeypatch, asr=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_bad_max_mb_is_clamped_for_enabled_asr(monkeypatch):
    """Мусор в CC_ML_ASR_MAX_MB: старт живой, запрос в пределах дефолта — 200."""
    monkeypatch.setenv("CC_ML_ASR_MAX_MB", "abc")
    app, _ = _app(monkeypatch)  # create_app не должен упасть
    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 200, response.text


def test_oversize_rejected_before_parsing_when_asr_disabled(monkeypatch):
    """Выключенная транскрипция не должна спулить 12 МиБ ради 503."""
    monkeypatch.setenv("CC_ML_ASR_MAX_MB", "5")
    app, _ = _app(monkeypatch, asr=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/transcribe",
            files={"file": ("session.webm", b"\x00" * (12 * 1024 * 1024), "audio/webm")},
        )
    assert response.status_code == 413, response.text
    assert _detail(response)["code"] == "payload_too_large"


# --- занятые слоты распознавания ------------------------------------------
def test_busy_when_all_slots_taken(monkeypatch):
    monkeypatch.setenv("CC_ML_ASR_CONCURRENCY", "1")
    app, registry = _app(monkeypatch)
    assert registry._asr_slots.acquire(blocking=False)  # noqa: SLF001 — эмулируем занятость
    try:
        with TestClient(app) as client:
            response = _post(client)
    finally:
        registry._asr_slots.release()  # noqa: SLF001
    assert response.status_code == 503, response.text
    assert _detail(response)["code"] == "busy"
    assert response.headers.get("Retry-After") == "5"


def test_concurrency_env_is_clamped(monkeypatch):
    monkeypatch.setenv("CC_ML_ASR_CONCURRENCY", "мусор")
    registry = Registry(stub=True)
    assert registry.asr_concurrency == 1
    monkeypatch.setenv("CC_ML_ASR_CONCURRENCY", "100")
    assert Registry(stub=True).asr_concurrency == 4


def test_second_concurrent_transcribe_gets_busy(monkeypatch):
    """Слот один: второй параллельный запрос получает 503 busy, а не ждёт."""
    monkeypatch.setenv("CC_ML_ASR_CONCURRENCY", "1")
    app, registry = _app(monkeypatch)
    started = threading.Event()
    release = threading.Event()

    class _Slow:
        model_id = "stub"
        device = "stub"

        def transcribe(self, source, language):
            started.set()
            release.wait(10)
            return {"duration_s": 1.0, "language": language, "segments": []}

    registry._asr = _Slow()  # noqa: SLF001 — медленный двойник бэкенда
    first: dict = {}
    with TestClient(app) as client:
        def _run() -> None:
            first["response"] = _post(client)

        worker = threading.Thread(target=_run)
        worker.start()
        assert started.wait(10), "первый запрос не дошёл до бэкенда"
        second = _post(client)
        release.set()
        worker.join(10)
    assert second.status_code == 503, second.text
    assert _detail(second)["code"] == "busy"
    assert second.headers.get("Retry-After") == "5"
    assert first["response"].status_code == 200, first["response"].text


def test_slot_released_after_backend_failure(monkeypatch):
    """Сбой бэкенда не должен навсегда съедать слот распознавания."""
    monkeypatch.setenv("CC_ML_ASR_CONCURRENCY", "1")
    app, registry = _app(monkeypatch)

    class _Broken:
        model_id = "stub"
        device = "stub"

        def transcribe(self, source, language):
            raise ValueError("не декодируется")

    registry._asr = _Broken()  # noqa: SLF001
    with TestClient(app) as client:
        for _ in range(3):
            assert _post(client).status_code == 422
        assert registry._asr_slots.acquire(blocking=False)  # noqa: SLF001
        registry._asr_slots.release()  # noqa: SLF001
        registry._asr = None  # noqa: SLF001 — вернуть stub-бэкенд
        registry.load_asr()
        assert _post(client).status_code == 200


def test_backend_receives_stream_at_start(monkeypatch):
    """Backend получает поток с позиции 0, а не после size-проверки."""
    app, registry = _app(monkeypatch)
    seen: dict = {}

    class _Probe:
        model_id = "stub"
        device = "stub"

        def transcribe(self, source, language):
            seen["tell"] = source.tell()
            return {
                "duration_s": 1.0,
                "language": language,
                "segments": [{"start": 0.0, "end": 1.0, "speaker": None, "text": "x"}],
            }

    registry._asr = _Probe()  # noqa: SLF001
    with TestClient(app) as client:
        assert _post(client).status_code == 200
    assert seen["tell"] == 0


def test_stub_rewinds_same_stream(monkeypatch):
    """Повторный вызов на том же потоке даёт ту же длительность."""

    stub = StubTranscriber()
    stream = io.BytesIO(_audio(12.0))
    first = stub.transcribe(stream, "ru")
    second = stub.transcribe(stream, "ru")
    assert first["duration_s"] == second["duration_s"] == 12.0
    assert len(second["segments"]) == len(first["segments"]) == 3


def test_405_keeps_allow_header(monkeypatch):
    """Конверт не должен терять заголовки Starlette: у 405 это Allow."""
    app, _ = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.get("/v1/transcribe")
    assert response.status_code == 405, response.text
    assert _detail(response)["code"] == "invalid_request"
    assert "POST" in response.headers.get("allow", "")
