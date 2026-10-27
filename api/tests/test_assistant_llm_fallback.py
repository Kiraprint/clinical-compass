# FR-17: ответ ассистента пишет LLM по сниппетам, но детерминированная выдача
# фрагментов не должна выглядеть как ответ ИИ. Контракт ответа несёт
# answer_kind/llm_reason/fragments (docs/03 §12); тесты — без сети, фейковый
# транспорт /chat/completions.
"""Причина отказа LLM и форма ответа ассистента: answer_kind, llm_reason, fragments."""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import pytest

from app.config import settings
from app.services import assistant_chat, llm_router
from app.services.agent import AgentFactory
from compass.agent import RAGChunk, build_rag_answer

from .conftest import clinician_ctx

CHUNKS = [
    RAGChunk(
        text="PHQ-9: 0–4 нет депрессии, 5–9 лёгкая, 10–14 умеренная.",
        source_title="Гайдлайн PHQ-9",
        guideline_id="who-mhgap-phq9",
        topic="ШКАЛЫ",
        score=0.81,
    ),
    RAGChunk(
        text="GAD-7: 10 и выше — умеренная тревога.",
        source_title="Гайдлайн GAD-7",
        guideline_id="who-mhgap-gad7",
        topic="ШКАЛЫ",
        score=0.7,
    ),
]

QUERY = "Какой порог тяжести у шкалы PHQ-9?"


@pytest.fixture(autouse=True)
def _llm_on(monkeypatch):
    """По умолчанию LLM включён: каждый тест сам решает, чем ответит транспорт."""
    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm.test/v1")
    monkeypatch.setattr(settings, "assistant_llm_api_key", "")


class _Resp:
    def __init__(self, body: dict) -> None:
        self._body = body

    def read(self) -> bytes:
        return json.dumps(self._body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_chat(
    monkeypatch,
    *,
    reply: str | None = None,
    finish_reason: str | None = "stop",
    raise_exc: BaseException | None = None,
) -> list[dict]:
    """Подменяет urlopen; возвращает тела ушедших запросов."""
    bodies: list[dict] = []

    def fake_urlopen(req, timeout=None):
        bodies.append({"url": req.full_url, "timeout": timeout, "body": json.loads(req.data)})
        if raise_exc is not None:
            raise raise_exc
        return _Resp(
            {"choices": [{"message": {"content": reply}, "finish_reason": finish_reason}]}
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return bodies


def _answer(**state) -> dict:
    """Прогон узла answer без Qdrant: только сборка ответа."""
    payload = {"query": QUERY, "chunks": CHUNKS, "history": [], **state}
    return AgentFactory(client=None)._answer(payload)["answer"]


def _factory_stream(monkeypatch, chunks=None) -> list[dict]:
    """События AgentFactory.stream без Qdrant (retrieve подменён)."""
    factory = AgentFactory(client=None)
    factory._state = lambda query, **kw: {  # type: ignore[method-assign]
        "query": query,
        "history": [],
        "chunks": CHUNKS if chunks is None else chunks,
        "topic": None,
    }
    factory._retrieve = lambda st: {}  # type: ignore[method-assign]

    async def collect() -> list[dict]:
        return [event async for event in factory.stream(QUERY)]

    return asyncio.run(collect())


def test_timeout_falls_back_to_fragments_with_reason(monkeypatch):
    """Таймаут шлюза → детерминированные фрагменты + llm_reason=timeout."""
    bodies = _fake_chat(monkeypatch, raise_exc=TimeoutError("timed out"))
    out = _answer()

    assert bodies, "запрос к LLM должен был уйти"
    assert out["llm_reason"] == "timeout"
    assert out["answer_kind"] == "fragments"
    # Текст и цитаты — детерминированная сборка, а не ответ модели.
    assert out["answer"] == build_rag_answer(QUERY, CHUNKS).answer
    # fragments ровно те же, что citations: номера, порядок и source_title.
    assert out["fragments"] == [
        {"n": n, "source_title": c["source_title"], "text": chunk.text}
        for n, (c, chunk) in enumerate(zip(out["citations"], CHUNKS), start=1)
    ]
    assert [c["source_title"] for c in out["citations"]] == [
        f["source_title"] for f in out["fragments"]
    ]


def test_empty_content_from_reasoning_model_is_empty(monkeypatch):
    """finish_reason=length с пустым content (reasoning-модель) → empty."""
    _fake_chat(monkeypatch, reply="", finish_reason="length")
    out = _answer()

    assert out["answer_kind"] == "fragments"
    assert out["llm_reason"] == "empty"
    assert out["fragments"], "UI должен показать фрагменты, а не пустоту"


def test_http_error_is_reported(monkeypatch):
    """Ошибка шлюза (URLError) → http_error, ответ всё равно есть."""
    _fake_chat(monkeypatch, raise_exc=urllib.error.URLError("connection refused"))
    out = _answer()

    assert out["llm_reason"] == "http_error"
    assert out["answer_kind"] == "fragments"


def test_happy_path_is_llm_answer(monkeypatch):
    """Модель написала связный ответ со ссылкой → answer_kind=llm, ok, fragments=[]."""
    _fake_chat(monkeypatch, reply="Порог умеренной депрессии — 10 баллов [1].")
    out = _answer()

    assert out["answer_kind"] == "llm"
    assert out["llm_reason"] == "ok"
    assert out["fragments"] == []
    assert out["answer"].startswith("Порог умеренной депрессии")
    assert out["citations"][0]["guideline_id"] == "who-mhgap-phq9"


def test_answer_without_valid_citations_is_empty(monkeypatch):
    """Ответ модели без ссылок на сниппеты считается пустым (нет grounded-текста)."""
    _fake_chat(monkeypatch, reply="Просто текст без источников.")
    out = _answer()

    assert out["answer_kind"] == "fragments"
    assert out["llm_reason"] == "empty"


def test_no_chunks_is_disabled_and_llm_not_called(monkeypatch):
    """Нет сниппетов → LLM не зовём, reason=disabled."""

    def boom(*args, **kwargs):
        raise AssertionError("LLM не должен вызываться без сниппетов")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    out = _answer(chunks=[])

    assert out["answer_kind"] == "fragments"
    assert out["llm_reason"] == "disabled"
    assert out["fragments"] == []


def test_max_tokens_is_sent_to_gateway(monkeypatch):
    """Бюджет генерации уходит в тело запроса: без него reasoning-модель молчит."""
    bodies = _fake_chat(monkeypatch, reply="Порог 10 [1].")

    assert llm_router.compose_answer([], QUERY, [{"text": "т", "source_title": "s"}]) == "Порог 10 [1]."
    assert bodies[0]["body"]["max_tokens"] == settings.assistant_llm_answer_max_tokens

    monkeypatch.setattr(settings, "assistant_llm_answer_max_tokens", 256)
    llm_router.compose_answer([], QUERY, [{"text": "т", "source_title": "s"}])
    assert bodies[1]["body"]["max_tokens"] == 256


def test_compose_answer_result_reasons(monkeypatch):
    """compose_answer_result отдаёт пару (текст, причина) для всех веток."""
    assert llm_router.compose_answer_result([], QUERY, []) == (None, "disabled")

    monkeypatch.setattr(settings, "assistant_llm_url", "")
    assert llm_router.compose_answer_result([], QUERY, [{"text": "т", "source_title": "s"}]) == (
        None,
        "disabled",
    )

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm.test/v1")
    _fake_chat(monkeypatch, reply="Ответ [1].")
    assert llm_router.compose_answer_result([], QUERY, [{"text": "т", "source_title": "s"}]) == (
        "Ответ [1].",
        "ok",
    )


def test_stream_timeout_marks_reason(monkeypatch):
    """Поток: обрыв по таймауту → последнее событие несёт llm_reason=timeout."""

    async def boom(history, query, chunks):
        raise TimeoutError("timed out")
        yield ""  # pragma: no cover — генератор должен быть асинхронным

    monkeypatch.setattr(llm_router, "compose_answer_stream", boom)
    events = _factory_stream(monkeypatch)

    answer = events[-1]["answer"]
    assert answer["llm_reason"] == "timeout"
    assert answer["answer_kind"] == "fragments"
    assert [f["n"] for f in answer["fragments"]] == [1, 2]
    assert [f["source_title"] for f in answer["fragments"]] == [
        c["source_title"] for c in answer["citations"]
    ]


def test_stream_happy_path_marks_ok(monkeypatch):
    """Поток: связный ответ со ссылкой → answer_kind=llm, llm_reason=ok."""

    async def pieces(history, query, chunks):
        yield "Порог умеренной "
        yield "депрессии — 10 [1]."

    monkeypatch.setattr(llm_router, "compose_answer_stream", pieces)
    events = _factory_stream(monkeypatch)

    assert "".join(e["delta"] for e in events if "delta" in e) == "Порог умеренной депрессии — 10 [1]."
    answer = events[-1]["answer"]
    assert (answer["answer_kind"], answer["llm_reason"]) == ("llm", "ok")
    assert answer["fragments"] == []


def test_stream_empty_output_marks_empty(monkeypatch):
    """Поток: модель ничего не написала → empty и фрагменты."""

    async def nothing(history, query, chunks):
        return
        yield ""  # pragma: no cover

    monkeypatch.setattr(llm_router, "compose_answer_stream", nothing)
    events = _factory_stream(monkeypatch)

    answer = events[-1]["answer"]
    assert (answer["answer_kind"], answer["llm_reason"]) == ("fragments", "empty")


def test_sse_stream_headers_disable_proxy_buffering(client):
    """Регресс «UI висит до конца генерации»: SSE отдаёт X-Accel-Buffering: no."""
    ch, _ = clinician_ctx(client, "sse-buffering@test.ru")
    tid = client.post(
        "/api/v1/assistant/threads", json={"title": "Буферизация"}, headers=ch
    ).json()["id"]

    resp = client.post(
        f"/api/v1/assistant/threads/{tid}/messages/stream",
        json={"content": "PHQ-9?"},
        headers=ch,
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-accel-buffering"] == "no"
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["connection"] == "keep-alive"
    assert resp.headers["content-encoding"] == "identity"  # gzip не копит SSE


def _fake_result(**overrides) -> dict:
    result = {
        "answer": "— PHQ-9: 10–14 умеренная депрессия [1].",
        "citations": [
            {
                "source_title": "Гайдлайн PHQ-9",
                "guideline_id": "who-mhgap-phq9",
                "topic": "ШКАЛЫ",
                "score": 0.81,
            }
        ],
        "topic": "ШКАЛЫ",
        "grounded": True,
        "refused": False,
        "refusal_code": None,
        "answer_kind": "fragments",
        "llm_reason": "timeout",
        "fragments": [
            {
                "n": 1,
                "source_title": "Гайдлайн PHQ-9",
                "text": "PHQ-9: 0–4 нет депрессии, 5–9 лёгкая, 10–14 умеренная.",
            }
        ],
    }
    result.update(overrides)
    return result


def test_message_payload_and_thread_detail_keep_answer_meta(client, monkeypatch):
    """answer_kind/llm_reason/fragments доходят до /messages и thread-detail."""
    ch, _ = clinician_ctx(client, "answer-meta@test.ru")
    tid = client.post("/api/v1/assistant/threads", json={"title": "Мета"}, headers=ch).json()["id"]

    async def fake_invoke(query: str, **kwargs) -> dict:
        return _fake_result()

    monkeypatch.setattr(assistant_chat, "_invoke_agent", fake_invoke)
    resp = client.post(
        f"/api/v1/assistant/threads/{tid}/messages",
        json={"content": "PHQ-9?"},
        headers=ch,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert (data["answer_kind"], data["llm_reason"]) == ("fragments", "timeout")
    assert data["fragments"] == _fake_result()["fragments"]

    detail = client.get(f"/api/v1/assistant/threads/{tid}", headers=ch).json()
    assert (detail["messages"][0]["answer_kind"], detail["messages"][0]["llm_reason"]) == (
        None,
        None,
    )
    assert detail["messages"][1]["answer_kind"] == "fragments"
    assert detail["messages"][1]["llm_reason"] == "timeout"
    assert detail["messages"][1]["fragments"] == _fake_result()["fragments"]


def test_stream_done_event_carries_answer_meta(client, monkeypatch):
    """SSE-событие done несёт те же поля, что и /messages."""
    ch, _ = clinician_ctx(client, "answer-meta-stream@test.ru")
    tid = client.post(
        "/api/v1/assistant/threads", json={"title": "Мета SSE"}, headers=ch
    ).json()["id"]

    async def fake_stream(query: str, **kwargs):
        yield {"delta": "— PHQ-9: "}
        yield {"answer": _fake_result()}

    monkeypatch.setattr(assistant_chat, "_stream_agent", fake_stream)
    resp = client.post(
        f"/api/v1/assistant/threads/{tid}/messages/stream",
        json={"content": "PHQ-9?"},
        headers=ch,
    )
    assert resp.status_code == 200
    done = next(
        json.loads(block.split("data: ", 1)[1])
        for block in resp.text.strip().split("\n\n")
        if block.startswith("event: done")
    )
    assert (done["answer_kind"], done["llm_reason"]) == ("fragments", "timeout")
    assert done["fragments"] == _fake_result()["fragments"]


def test_fragments_align_with_citations_for_duplicates_and_patient_card(monkeypatch):
    """Дубль guideline_id и карта пациента в citations не сдвигают номера фрагментов."""
    dup = RAGChunk(
        text="PHQ-9 (повтор): 10–14 умеренная.",
        source_title="Гайдлайн PHQ-9",
        guideline_id="who-mhgap-phq9",
        topic="ШКАЛЫ",
        score=0.75,
    )
    _fake_chat(monkeypatch, raise_exc=TimeoutError("timed out"))
    out = _answer(
        chunks=[CHUNKS[0], dup, CHUNKS[1]],
        patient_contexts=[{"patient_id": "p-1", "fio": "Иванов"}],
    )

    assert [c["guideline_id"] for c in out["citations"]] == [
        "who-mhgap-phq9",
        "who-mhgap-gad7",
        "patient:p-1",
    ]
    # Карта пациента — не фрагмент КБ; номера первых двух остаются 1 и 2.
    assert out["fragments"] == [
        {"n": 1, "source_title": "Гайдлайн PHQ-9", "text": CHUNKS[0].text},
        {"n": 2, "source_title": "Гайдлайн GAD-7", "text": CHUNKS[1].text},
    ]
    assert [c["source_title"] for c in out["citations"][:2]] == [
        f["source_title"] for f in out["fragments"]
    ]
