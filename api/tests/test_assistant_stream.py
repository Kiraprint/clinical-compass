# FR-17: поток токенов ассистента (SSE). Контракт ответа = AssistantThreadAnswer.
"""SSE-поток ответа ассистента: парсер LLM, события и персистенция."""
from __future__ import annotations

import asyncio
import json

from app.config import settings
from app.services import assistant_chat, llm_router

from .conftest import clinician_ctx

CHUNKS = [{"text": "PHQ-9: 10–14 умеренная депрессия.", "source_title": "Гайдлайн"}]

def _events(text: str) -> list[dict]:
    events: list[dict] = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        name = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        events.append({"event": name, "data": data})
    return events

def test_compose_answer_stream_parses_sse(monkeypatch):
    """stream:true → delta.content по порядку; [DONE] завершает поток."""
    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm.test/v1")
    monkeypatch.setattr(settings, "assistant_llm_api_key", "")
    seen: list[dict] = []

    class Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self):
            for line in (
                'data: {"choices":[{"delta":{"content":"Порог "}}]}',
                'data: {"choices":[{"delta":{"content":"10 [1]"},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                'data: {"choices":[],"usage":{"total_tokens":42}}',
                "data: [DONE]",
            ):
                yield line

    class Client:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, *, json, headers):
            seen.append(json)
            return Resp()

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", Client)

    async def collect():
        return [p async for p in llm_router.compose_answer_stream([], "PHQ-9?", list(CHUNKS))]

    assert "".join(asyncio.run(collect())) == "Порог 10 [1]"
    assert seen[0]["stream"] is True

def test_stream_endpoint_tokens_done_and_persists(client, monkeypatch):
    """token-события идут до done; оба сообщения сохранены в треде."""
    ch, _ = clinician_ctx(client, "stream-c@test.ru")
    tid = client.post("/api/v1/assistant/threads", json={"title": "Поток"}, headers=ch).json()["id"]

    async def fake_stream(query: str, **kwargs):
        yield {"delta": "По данным "}
        yield {"delta": "базы знаний [1]."}
        yield {
            "answer": {
                "answer": "По данным базы знаний [1].",
                "grounded": True,
                "refused": False,
                "refusal_code": None,
                "topic": "ШКАЛЫ",
                "citations": [
                    {"source_title": "Гайдлайн", "guideline_id": "g1", "topic": "ШКАЛЫ", "score": 0.9}
                ],
            }
        }

    monkeypatch.setattr(assistant_chat, "_stream_agent", fake_stream)
    resp = client.post(
        f"/api/v1/assistant/threads/{tid}/messages/stream",
        json={"content": "PHQ-9?"},
        headers=ch,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["content-encoding"] == "identity"  # gzip не копит SSE
    events = _events(resp.text)
    assert [e["event"] for e in events] == ["meta", "token", "token", "done"]
    assert "".join(e["data"]["t"] for e in events if e["event"] == "token") == "По данным базы знаний [1]."
    done = events[-1]["data"]
    assert done["answer"] == "По данным базы знаний [1]."
    assert done["citations"][0]["guideline_id"] == "g1"
    assert done["message_ids"] and len(done["message_ids"]) == 2

    detail = client.get(f"/api/v1/assistant/threads/{tid}", headers=ch).json()
    assert [m["role"] for m in detail["messages"]] == ["CLINICIAN", "ASSISTANT"]
    assert detail["messages"][1]["content"] == done["answer"]

def test_stream_foreign_thread_is_error_event(client):
    """IDOR: чужой тред → событие error с THREAD_NOT_FOUND (статус уже 200)."""
    ch_a, _ = clinician_ctx(client, "stream-a@test.ru")
    ch_b, _ = clinician_ctx(client, "stream-b@test.ru")
    tid = client.post("/api/v1/assistant/threads", json={"title": "A"}, headers=ch_a).json()["id"]
    resp = client.post(
        f"/api/v1/assistant/threads/{tid}/messages/stream",
        json={"content": "?"},
        headers=ch_b,
    )
    assert resp.status_code == 200
    events = _events(resp.text)
    assert events == [
        {"event": "error", "data": {"code": "THREAD_NOT_FOUND", "detail": "Диалог не найден", "status": 404}}
    ]

def test_agent_stream_cleans_markdown_before_done(monkeypatch):
    """Поток и batch дают одинаковый чистый текст: ``` убираются до done."""
    from compass.agent import RAGChunk
    from app.services.agent import AgentFactory

    chunk = RAGChunk(
        text="PHQ-9: 10–14 умеренная.",
        source_title="Гайдлайн",
        guideline_id="g1",
        topic="ШКАЛЫ",
        score=0.9,
    )
    factory = AgentFactory(client=None)
    monkeypatch.setattr(factory, "_state", lambda *a, **kw: {"query": "PHQ-9?", "history": []})
    monkeypatch.setattr(factory, "_retrieve", lambda state: {"chunks": [chunk]})

    async def fake_parts(history, query, chunks, **kwargs):
        yield "```markdown\n"
        yield "По базе знаний [1].\n"
        yield "```"

    monkeypatch.setattr(llm_router, "compose_answer_stream", fake_parts)

    async def collect():
        return [item async for item in factory.stream("PHQ-9?")]

    events = asyncio.run(collect())
    assert events[-1]["answer"]["answer"] == "По базе знаний [1]."
    assert events[-1]["answer"]["citations"][0]["guideline_id"] == "g1"

def test_stream_empty_agent_answer_is_domain_error(client, monkeypatch):
    """Пустой ответ агента — доменный ASSISTANT_NO_ANSWER, а не 500-заглушка."""
    async def empty(*args, **kwargs):
        if False:
            yield {}

    monkeypatch.setattr(assistant_chat, "_stream_agent", empty)
    ch, _ = clinician_ctx(client, "stream-empty@test.ru")
    tid = client.post("/api/v1/assistant/threads", json={}, headers=ch).json()["id"]
    resp = client.post(
        f"/api/v1/assistant/threads/{tid}/messages/stream",
        json={"content": "PHQ-9?"},
        headers=ch,
    )
    events = _events(resp.text)
    assert [e["event"] for e in events] == ["meta", "error"]
    assert events[1]["data"]["code"] == "ASSISTANT_NO_ANSWER"
    assert events[1]["data"]["status"] == 500


def test_stream_body_carries_anonymized_patient_card(monkeypatch):
    """Поток шлёт тот же блок карты, что batch: без ФИО, с маской [ПАЦИЕНТ]."""
    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm.test/v1")
    monkeypatch.setattr(settings, "assistant_llm_api_key", "")
    seen: list[dict] = []

    class Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self):
            for line in (
                'data: {"choices":[{"delta":{"content":"Балл 17 [П1]"}}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                "data: [DONE]",
            ):
                yield line

    class Client:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, *, json, headers):
            seen.append(json)
            return Resp()

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", Client)
    card = {
        "patient_id": "p-1",
        "fio": "Петрова Анна Сергеевна",
        "age": 41,
        "prescribed_scales": ["PHQ-9"],
        "last_assessment": {
            "total_score": 17,
            "severity": "умеренная",
            "triage": "YELLOW",
            "computed_at": "2026-10-20T09:00:00Z",
        },
        "trend": {"moving_avg": [17.0], "delta_7": 2},
        "open_escalations": [],
        "recent_diary": [],
    }

    async def collect():
        return [
            piece
            async for piece in llm_router.compose_answer_stream(
                [], "PHQ-9?", list(CHUNKS), patient_contexts=[card]
            )
        ]

    assert "".join(asyncio.run(collect())) == "Балл 17 [П1]"
    prompt = seen[0]["messages"][1]["content"]
    assert "Карта пациента (обезлично):" in prompt
    assert "П1: пациент: [ПАЦИЕНТ]" in prompt
    assert "последняя оценка: 17 (умеренная)" in prompt
    sent = json.dumps(seen[0], ensure_ascii=False)
    assert "Петрова" not in sent
    assert "p-1" not in sent
