"""Контракт ML-сайдкара (api/app/services/ml_client.py) в stub-режиме."""

from __future__ import annotations

import pytest

from app import create_app
from models import MAX_CHARS, MAX_DOCUMENTS, MAX_TEXTS, HybridMasker, Registry

def _detail(response) -> dict:
    body = response.json()
    assert set(body) == {"detail"}, body
    assert set(body["detail"]) == {"code", "message"}, body
    return body["detail"]


# --- /health и 503 во время загрузки -------------------------------------


def test_health_loading(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "loading"
    assert body["device"] == "stub"
    assert body["stub"] is True


def test_endpoints_503_while_loading(client):
    for path, payload in (
        ("/v1/embeddings", {"texts": ["тест"]}),
        ("/v1/rerank", {"query": "q", "documents": ["d"]}),
        ("/v1/mask", {"text": "тел. +7 916 123-45-67"}),
    ):
        response = client.post(path, json=payload)
        assert response.status_code == 503, path
        assert _detail(response)["code"] == "model_loading"
        assert response.headers.get("Retry-After") == "10"


def test_health_ready(ready):
    body = ready.get("/health").json()
    assert body["status"] == "ready"
    assert body["models"]["embeddings"]["default"] == "qwen3-0.6b"
    assert body["models"]["embeddings"]["loaded"] == ["qwen3-0.6b"]


def test_invalid_default_profile_rejected():
    from models import Registry

    # Опечатка в CC_ML_DEFAULT_PROFILE не должна доходить до запросов.
    with pytest.raises(RuntimeError, match="отсутствует в профилях"):
        Registry(stub=True, default_profile="nope")


def test_models_unavailable_code(registry):
    from fastapi.testclient import TestClient

    from app import create_app

    registry._status = "error"  # noqa: SLF001 — имитация упавшей загрузки
    registry._error = "torch: CUDA out of memory"  # noqa: SLF001
    with TestClient(create_app(registry, start_warmup=False)) as client:
        response = client.post("/v1/embeddings", json={"texts": ["x"]})
        assert response.status_code == 503
        assert _detail(response)["code"] == "model_unavailable"


def test_token_missing_401(monkeypatch, registry):
    monkeypatch.setenv("CC_ML_TOKEN", "s3cret")
    from fastapi.testclient import TestClient

    from app import create_app

    registry.warmup()
    with TestClient(create_app(registry, start_warmup=False)) as client:
        response = client.post("/v1/embeddings", json={"texts": ["a"]})
        assert response.status_code == 401
        assert _detail(response)["code"] == "unauthorized"


def test_token_wrong_403(monkeypatch, registry):
    monkeypatch.setenv("CC_ML_TOKEN", "s3cret")
    from fastapi.testclient import TestClient

    from app import create_app

    registry.warmup()
    with TestClient(create_app(registry, start_warmup=False)) as client:
        response = client.post(
            "/v1/embeddings",
            json={"texts": ["a"]},
            headers={"X-CC-ML-Token": "wrong"},
        )
        assert response.status_code == 403
        assert _detail(response)["code"] == "forbidden"


def test_token_ok(monkeypatch, registry):
    monkeypatch.setenv("CC_ML_TOKEN", "s3cret")
    from fastapi.testclient import TestClient

    from app import create_app

    registry.warmup()
    with TestClient(create_app(registry, start_warmup=False)) as client:
        response = client.post(
            "/v1/embeddings",
            json={"texts": ["a"]},
            headers={"X-CC-ML-Token": "s3cret"},
        )
        assert response.status_code == 200
        # /health без токена — liveness, ПДн не отдаёт.
        assert client.get("/health").status_code == 200


# --- embeddings -----------------------------------------------------------


def test_embeddings_shape_and_dims(ready):
    response = ready.post(
        "/v1/embeddings",
        json={"texts": ["тревога", "депрессия"], "kind": "query", "profile": "qwen3-0.6b"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["profile"] == "qwen3-0.6b"
    assert body["dense_size"] == 1024
    assert len(body["vectors"]) == 2
    assert all(len(v) == 1024 for v in body["vectors"])
    # L2-норма — как у настоящего эмбеддера (косинус = скалярное произведение).
    assert sum(x * x for x in body["vectors"][0]) == pytest.approx(1.0, abs=1e-6)


def test_embeddings_default_profile(ready):
    body = ready.post("/v1/embeddings", json={"texts": ["x"]}).json()
    assert body["profile"] == "qwen3-0.6b"


def test_embeddings_query_and_passage_differ(ready):
    q = ready.post("/v1/embeddings", json={"texts": ["тревога"], "kind": "query"})
    p = ready.post("/v1/embeddings", json={"texts": ["тревога"], "kind": "passage"})
    assert q.json()["vectors"] != p.json()["vectors"]


def test_embeddings_second_profile(ready):
    body = ready.post(
        "/v1/embeddings", json={"texts": ["x"], "profile": "e5-large-instruct"}
    ).json()
    assert body["profile"] == "e5-large-instruct"
    assert body["dense_size"] == 1024


def test_embeddings_unknown_profile(ready):
    response = ready.post("/v1/embeddings", json={"texts": ["x"], "profile": "nope"})
    assert response.status_code == 404
    assert _detail(response)["code"] == "unknown_profile"


# --- лимиты входа ---------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"texts": ["x"] * (MAX_TEXTS + 1)},
        {"texts": ["x" * (MAX_CHARS + 1)]},
        {"texts": []},
        {"texts": ["ok"], "kind": "document"},
    ],
)
def test_embeddings_caps(ready, payload):
    response = ready.post("/v1/embeddings", json=payload)
    assert response.status_code == 422
    assert _detail(response)["code"] == "invalid_request"


def test_rerank_caps(ready):
    too_many = ready.post(
        "/v1/rerank", json={"query": "q", "documents": ["d"] * (MAX_DOCUMENTS + 1)}
    )
    assert too_many.status_code == 422
    long_doc = ready.post(
        "/v1/rerank", json={"query": "q", "documents": ["d" * (MAX_CHARS + 1)]}
    )
    assert long_doc.status_code == 422
    bad_top_k = ready.post(
        "/v1/rerank", json={"query": "q", "documents": ["d"], "top_k": 5}
    )
    assert bad_top_k.status_code == 422
    assert _detail(bad_top_k)["code"] == "invalid_request"


def test_mask_cap(ready):
    response = ready.post("/v1/mask", json={"text": "x" * (MAX_CHARS + 1)})
    assert response.status_code == 422
    assert _detail(response)["code"] == "invalid_request"


# --- реранк ---------------------------------------------------------------


def test_rerank_alignment(ready):
    documents = [
        "побочный эффект: головная боль",
        "серотониновый синдром при приёме СИОЗС",
        "дозировка препарата",
    ]
    response = ready.post(
        "/v1/rerank",
        json={"query": "серотониновый синдром СИОЗС", "documents": documents},
    )
    assert response.status_code == 200
    scores = response.json()["scores"]
    assert len(scores) == len(documents)
    assert all(isinstance(s, float) for s in scores)
    # stub: документ с максимальным пересечением — лучший; порядок = вход.
    assert scores.index(max(scores)) == 1


# --- маскирование ---------------------------------------------------------


def _mask(ready, text: str, names: list[str] | None = None) -> dict:
    response = ready.post("/v1/mask", json={"text": text, "names": names or []})
    assert response.status_code == 200, response.text
    return response.json()


def test_mask_spans_and_text(ready):
    text = (
        "Пациент Иванов Иван Иванович, тел. +7 916 123-45-67, "
        "дата рождения: 12.03.1975, СНИЛС 123-456-789 00. Жалобы: тревога."
    )
    body = _mask(ready, text, ["Иванов Иван Иванович"])
    # stub — явный режим без NER: model=regex и это не деградация.
    assert body["model"] == "regex"
    assert body["degraded"] is False
    spans = body["spans"]
    assert spans == sorted(spans, key=lambda s: (s["start"], s["end"]))
    categories = {s["category"] for s in spans}
    assert {"FIO", "PHONE", "DOB", "DOC"} <= categories
    for span in spans:
        assert 0 <= span["start"] < span["end"] <= len(text)
        # Границы указывают на исходные ПДн, а не на placeholder.
        original = text[span["start"] : span["end"]]
        assert original not in ("[ФИО]", "[ТЕЛЕФОН]", "[ДАТА РОЖДЕНИЯ]", "[ДОКУМЕНТ]")
    assert "Иванов" not in body["text"]
    assert "+7 916 123-45-67" not in body["text"]
    assert "[ФИО]" in body["text"]
    assert "Жалобы: тревога" in body["text"]


def test_mask_without_names_keeps_regex_only(ready):
    body = _mask(ready, "Позвонить +7 (916) 123-45-67 в 15:00.")
    assert [s["category"] for s in body["spans"]] == ["PHONE"]
    assert "15:00" in body["text"]

# --- старт без токена, потолок тела, отсутствие утечек ---------------------

def test_startup_requires_token(monkeypatch):
    monkeypatch.delenv("CC_ML_TOKEN", raising=False)
    monkeypatch.delenv("CC_ML_ALLOW_ANONYMOUS", raising=False)
    from models import Registry

    with pytest.raises(RuntimeError, match="CC_ML_TOKEN"):
        create_app(Registry(stub=True), start_warmup=False)

def test_startup_allows_explicit_anonymous(monkeypatch):
    monkeypatch.delenv("CC_ML_TOKEN", raising=False)
    monkeypatch.setenv("CC_ML_ALLOW_ANONYMOUS", "1")
    app = create_app(Registry(stub=True), start_warmup=False)
    assert app is not None

def test_body_too_large_rejected_before_parsing(client):
    # Валидный по схеме текст сюда не доходит: Content-Length > лимита /v1/mask.
    import json

    payload = json.dumps({"text": "я" * 700_000})
    response = client.post(
        "/v1/mask",
        content=payload.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert _detail(response)["code"] == "payload_too_large"

def test_health_error_has_no_details(registry):
    from fastapi.testclient import TestClient

    registry._status = "error"  # noqa: SLF001
    registry._error = "OSError: /home/user/.cache/huggingface/secret"  # noqa: SLF001
    with TestClient(create_app(registry, start_warmup=False)) as client:
        body = client.get("/health").json()
    assert body["error"] is True
    assert "secret" not in str(body)
    assert "huggingface" not in str(body)

# --- NER: длинный текст, окна и честный degraded -------------------------

def _fake_masker(pipe) -> HybridMasker:
    """HybridMasker без torch: подменяем только pipeline."""
    import threading

    masker = object.__new__(HybridMasker)
    masker._pipe = pipe  # noqa: SLF001
    masker._lock = threading.Lock()  # noqa: SLF001
    masker._broken = False  # noqa: SLF001
    return masker

def _name_pipe(chunk: str) -> list[dict]:
    pos = chunk.find("Петров")
    return [] if pos < 0 else [{"entity_group": "LAST_NAME", "start": pos, "end": pos + 6}]

def test_ner_chunks_cover_long_text():
    from models import NER_CHUNK_CHARS, NER_CHUNK_OVERLAP

    text = ("Пациент пришёл на приём. " * 300) + "Петров Пётр. Конец."
    assert len(text) > 2 * NER_CHUNK_CHARS
    chunks = HybridMasker._chunks(text)
    assert chunks[0][0] == 0 and chunks[-1][1] == len(text)
    for start, end in chunks:
        assert end - start <= NER_CHUNK_CHARS
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
        # Перекрытие есть и близко к NER_CHUNK_OVERLAP (начало сдвигается
        # вперёд до границы слова, поэтому чуть меньше номинала).
        assert 0 < prev_end - next_start
        assert prev_end - next_start >= NER_CHUNK_OVERLAP - 100
    # Хвост текста (имя) обязан попасть в последнее окно целиком.
    assert "Петров" in text[chunks[-1][0] : chunks[-1][1]]

def test_ner_finds_name_in_last_chunk():
    text = ("Пациент пришёл на приём. " * 300) + "Петров Пётр. Конец."
    masker = _fake_masker(_name_pipe)
    body = masker.mask(text, [])
    assert body["model"] == "hybrid"
    assert body["degraded"] is False
    assert [s["category"] for s in body["spans"]].count("FIO") == 1
    assert "Петров" not in body["text"]
    assert "[ФИО]" in body["text"]

def test_ner_failure_is_degraded_and_sticky():
    def boom(chunk: str) -> list[dict]:
        raise RuntimeError("CUDA out of memory")

    masker = _fake_masker(boom)
    text = "Пациент Петров Пётр, тел. +7 916 123-45-67."
    body = masker.mask(text, [])
    assert body["model"] == "regex"
    assert body["degraded"] is True
    assert body["spans"], "regex-спаны должны остаться"
    assert masker._broken is True  # noqa: SLF001
    # После сбоя NER не пытаемся снова: запрос всё равно degraded.
    again = masker.mask(text, [])
    assert again["degraded"] is True
    assert again["model"] == "regex"

def test_health_hides_model_paths():
    # Локальный путь из env не должен уходить в /health даже после загрузки.
    from models import public_model_name

    assert public_model_name("/home/user/weights/rubert") == "rubert"
    assert public_model_name("~/cache/model") == "model"
    assert public_model_name("BAAI/bge-reranker-v2-m3") == "BAAI/bge-reranker-v2-m3"

    reg = Registry(stub=True, rerank_model="/srv/secret/rerank", mask_model="/srv/secret/ner")
    reg._reranker = object()  # noqa: SLF001 — как будто загружен
    reg._masker = object()  # noqa: SLF001
    body = reg.status()
    assert body["models"]["rerank"] == "rerank"
    assert body["models"]["mask"] == "ner"
    assert "/srv" not in str(body)
