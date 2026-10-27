# FR-17: ответ ассистента пишет LLM по сниппетам, но детерминированная выдача
# фрагментов не должна выглядеть как ответ ИИ. Контракт ответа несёт
# answer_kind/llm_reason/fragments (docs/03 §12); тесты — без сети, фейковый
# транспорт /chat/completions.
"""Причина отказа LLM и форма ответа ассистента: answer_kind, llm_reason, fragments."""
from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

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
    """Нет сниппетов → LLM не зовём: ответ по карте disabled, а без неё — отказ."""

    def boom(*args, **kwargs):
        raise AssertionError("LLM не должен вызываться без сниппетов")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    # Карта пациента есть, находок в КБ нет: детерминированный ответ по карте.
    card = _answer(chunks=[], patient_contexts=[{"patient_id": "p-1", "fio": "Иванов"}])
    assert (card["answer_kind"], card["llm_reason"]) == ("fragments", "disabled")
    assert card["fragments"] == []

    # Ни карты, ни находок — отказа не выдавать фрагментами (см. refusal-тесты).
    empty = _answer(chunks=[])
    assert (empty["answer_kind"], empty["llm_reason"]) == ("refusal", None)


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


def test_connect_timeout_is_timeout_not_http_error(monkeypatch):
    """URLError(reason=TimeoutError) — connect-timeout шлюза, не ошибка ответа."""
    _fake_chat(monkeypatch, raise_exc=urllib.error.URLError(TimeoutError("timed out")))
    out = _answer()
    assert (out["answer_kind"], out["llm_reason"]) == ("fragments", "timeout")
    assert out["fragments"]
    # socket.timeout — алиас TimeoutError; вложенность в URLError не важна.
    assert (
        llm_router.stream_error_reason(urllib.error.URLError(socket.timeout("timed out")))
        == "timeout"
    )
    # Отказ соединения таймаутом не считается.
    refused = urllib.error.URLError(ConnectionRefusedError("refused"))
    assert llm_router.stream_error_reason(refused) == "http_error"


def test_truncated_answer_is_not_shown_as_llm(monkeypatch):
    """finish_reason=length с непустым текстом — обрыв, а не ответ ИИ."""
    _fake_chat(
        monkeypatch,
        reply="PHQ-9: 0–4 нет депрессии, 5–9 лёгкая, 10–14 [1]. При баллах выше",
        finish_reason="length",
    )
    out = _answer()

    assert (out["answer_kind"], out["llm_reason"]) == ("fragments", "truncated")
    assert out["fragments"] and out["fragments"][0]["n"] == 1
    assert "При баллах выше" not in out["answer"]


def test_stream_truncated_marks_reason(monkeypatch):
    """Поток, упёршийся в max_tokens, отдаёт truncated и детерминированный ответ."""

    class Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"PHQ-9: 0–4 [1], 5–9"}}]}'
            yield 'data: {"choices":[{"delta":{},"finish_reason":"length"}]}'

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *args, **kwargs):
            return Resp()

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", Client)
    events = _factory_stream(monkeypatch)

    assert events[0] == {"delta": "PHQ-9: 0–4 [1], 5–9"}
    answer = events[-1]["answer"]
    assert (answer["answer_kind"], answer["llm_reason"]) == ("fragments", "truncated")
    assert answer["fragments"]


def test_refusal_is_its_own_answer_kind(monkeypatch):
    """Отказ — это answer_kind=refusal без плашки «ИИ недоступен», а не fragments."""
    bodies = _fake_chat(monkeypatch, reply="— PHQ-9: 10–14 умеренная [1].")
    out = _answer(chunks=[])

    # NO_KB_DATA — отказ без красной линии: refusal_code есть, refused=False.
    assert out["refusal_code"] == "NO_KB_DATA"
    assert (out["answer_kind"], out["llm_reason"]) == ("refusal", None)
    assert out["fragments"] == []
    assert bodies == [], "LLM не звали — вопрос не покрыт КБ"


def test_redline_refusal_has_no_llm_reason(monkeypatch):
    """Red line отвечается отказом, даже если КБ что-то нашла."""
    bodies = _fake_chat(monkeypatch, reply="— PHQ-9: 10–14 умеренная [1].")
    out = _answer(query="Какую дозу сертралина принимать?")

    assert out["refused"] is True
    assert (out["answer_kind"], out["llm_reason"]) == ("refusal", None)
    assert out["fragments"] == []
    assert bodies == []


def test_stream_refusal_has_no_llm_reason(monkeypatch):
    """Поток на пустой выдаче тоже отдаёт refusal, а не fragments/disabled."""
    _fake_chat(monkeypatch, reply="— PHQ-9: 10–14 умеренная [1].")
    answer = _factory_stream(monkeypatch, chunks=[])[-1]["answer"]

    assert answer["refusal_code"] == "NO_KB_DATA"
    assert (answer["answer_kind"], answer["llm_reason"]) == ("refusal", None)
    assert answer["fragments"] == []


def test_docs_claim_nginx_fix_only_via_ui_pr():
    """Утверждение о proxy_buffering off честно: правка приходит из PR #116."""
    root = Path(__file__).resolve().parents[2]
    docs = (root / "docs" / "03-архитектура.md").read_text(encoding="utf-8")
    bullet = next(line for line in docs.splitlines() if "proxy_buffering off" in line)
    assert "ui/nginx.conf" in bullet and "#116" in bullet

    router = (root / "api" / "app" / "routers" / "assistant.py").read_text(encoding="utf-8")
    assert "ui/nginx.conf" in router and "PR #116" in router
    assert "proxy_buffering off в ui/nginx.conf" not in router


def test_answer_meta_server_default_matches_migration():
    """server_default модели и миграции совпадают — иначе дрейф схемы."""
    from app.models import AssistantMessage

    column = AssistantMessage.__table__.c.answer_meta
    assert column.server_default is not None
    assert str(column.server_default.arg) == "'{}'::jsonb"

    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "b8c9d0e1f2a3_assistant_answer_meta.py"
    ).read_text(encoding="utf-8")
    assert str(column.server_default.arg) in migration


def test_stream_eof_without_terminal_chunk_is_truncated(monkeypatch):
    """Обрыв соединения без finish_reason/[DONE] — недописанный ответ, а не ok."""

    class Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self):
            # Прокси закрыл поток посреди генерации: терминального чанка нет.
            yield 'data: {"choices":[{"delta":{"content":"PHQ-9: 0–4 нет [1], "}}]}'
            yield 'data: {"choices":[{"delta":{"content":"5–9 лёгкая, 10–14"}}]}'

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *args, **kwargs):
            return Resp()

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", Client)
    events = _factory_stream(monkeypatch)

    answer = events[-1]["answer"]
    assert (answer["answer_kind"], answer["llm_reason"]) == ("fragments", "truncated")
    assert answer["fragments"]


def test_stream_truncated_persists_deterministic_answer(client, monkeypatch):
    """Обрыв потока: в тред пишется детерминированный ответ, а не обрубок модели."""
    ch, _ = clinician_ctx(client, "truncated-stream@test.ru")
    tid = client.post(
        "/api/v1/assistant/threads", json={"title": "Обрыв"}, headers=ch
    ).json()["id"]

    async def fake_stream(query: str, **kwargs):
        yield {"delta": "PHQ-9: 0–4 нет [1], 5–9"}
        yield {"answer": _fake_result(llm_reason="truncated")}

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
    assert (done["answer_kind"], done["llm_reason"]) == ("fragments", "truncated")

    detail = client.get(f"/api/v1/assistant/threads/{tid}", headers=ch)
    assert detail.status_code == 200, detail.text
    assistant = [m for m in detail.json()["messages"] if m["role"] == "ASSISTANT"]
    assert len(assistant) == 1
    stored = assistant[0]
    # Хранится и показывается ровно детерминированный ответ, а не обрубок токенов.
    assert stored["content"] == _fake_result()["answer"]
    assert stored["content"] != "PHQ-9: 0–4 нет [1], 5–9"
    assert (stored["answer_kind"], stored["llm_reason"]) == ("fragments", "truncated")
    assert stored["fragments"] == _fake_result()["fragments"]
