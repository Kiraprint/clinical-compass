"""Бюджет прогона ассистента: таймауты LLM ограничены остатком дедлайна.

Инвариант (docs/03 §12): `CC_ASSISTANT_DEADLINE_S` — бюджет всего прогона, а
роутер и ответ идут последовательно. Без общего бюджета дефолтные таймауты LLM
(120 с) могли пережить дедлайн, и прогон срывался в 503 ASSISTANT_TIMEOUT вместо
честной деградации до детерминированных фрагментов.

Тесты без сети: транспорт подменяется, Qdrant не трогается.
"""
from __future__ import annotations

import time

import pytest

from app.config import settings
from app.services import llm_router
from app.services.agent import AgentFactory, _time_left
from compass.agent import RAGChunk

CHUNKS = [
    RAGChunk(
        text="PHQ-9: 0–4 нет депрессии, 5–9 лёгкая, 10–14 умеренная.",
        source_title="Гайдлайн PHQ-9",
        guideline_id="who-mhgap-phq9",
        topic="ШКАЛЫ",
        score=0.81,
    ),
]


@pytest.fixture(autouse=True)
def _llm_on(monkeypatch):
    """LLM считается настроенным: иначе тело запроса не собирается вовсе."""
    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm.test/v1")

QUERY = "Какой порог тяжести у шкалы PHQ-9?"


def test_time_left_without_deadline_and_expired():
    assert _time_left(None) is None
    assert _time_left(time.monotonic() - 5) == 0.0
    left = _time_left(time.monotonic() + 30)
    assert left is not None and 29 <= left <= 30


def test_answer_skips_llm_when_budget_spent(monkeypatch):
    """Дедлайн истёк до ответа: LLM не зовём, отдаём фрагменты с причиной timeout."""
    called = []

    def _boom(*_args, **_kwargs):
        called.append(1)
        raise AssertionError("LLM не должен вызываться при исчерпанном бюджете")

    monkeypatch.setattr(llm_router, "compose_answer_result", _boom)
    state = {
        "query": QUERY,
        "chunks": CHUNKS,
        "history": [],
        "deadline_at": time.monotonic() - 1,
    }
    answer = AgentFactory(client=None)._answer(state)["answer"]
    assert called == []
    assert answer["answer_kind"] == "fragments"
    assert answer["llm_reason"] == "timeout"
    assert answer["fragments"], "детерминированные фрагменты обязаны остаться"


def test_answer_caps_timeout_by_remaining_budget(monkeypatch):
    """Настроенный таймаут 120 с не должен пережить дедлайн: уходит остаток."""
    seen: list[float | None] = []

    def _capture(_history, _query, _payload, *, timeout=None):
        seen.append(timeout)
        return None, "timeout"

    monkeypatch.setattr(settings, "assistant_llm_answer_timeout", 120.0)
    monkeypatch.setattr(llm_router, "compose_answer_result", _capture)
    state = {
        "query": QUERY,
        "chunks": CHUNKS,
        "history": [],
        "deadline_at": time.monotonic() + 2,
    }
    answer = AgentFactory(client=None)._answer(state)["answer"]
    assert seen and seen[0] is not None and seen[0] <= 2.0
    assert answer["answer_kind"] == "fragments"
    assert answer["llm_reason"] == "timeout"


def test_answer_without_deadline_uses_configured_timeout(monkeypatch):
    """Вне ассистента (нет дедлайна) поведение прежнее: таймаут из настроек."""
    seen: list[float | None] = []

    def _capture(_history, _query, _payload, *, timeout=None):
        seen.append(timeout)
        return None, "http_error"

    monkeypatch.setattr(llm_router, "compose_answer_result", _capture)
    state = {"query": QUERY, "chunks": CHUNKS, "history": []}
    AgentFactory(client=None)._answer(state)
    assert seen == [None]


def test_router_skipped_when_budget_spent(monkeypatch):
    """Роутинг после исчерпания бюджета не зовёт шлюз: темы пустые (поиск по всем)."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("роутер не должен вызываться при исчерпанном бюджете")

    monkeypatch.setattr(llm_router, "route_topics", _boom)
    state = AgentFactory(client=None)._state(
        QUERY, deadline_at=time.monotonic() - 1
    )
    assert state["topics"] == ()


def test_router_timeout_capped_by_budget(monkeypatch):
    seen: list[float | None] = []

    def _capture(_query, *, timeout=None):
        seen.append(timeout)
        return ("ШКАЛЫ",)

    monkeypatch.setattr(settings, "assistant_llm_timeout", 120.0)
    monkeypatch.setattr(llm_router, "route_topics", _capture)
    state = AgentFactory(client=None)._state(QUERY, deadline_at=time.monotonic() + 3)
    assert seen and seen[0] is not None and seen[0] <= 3.0
    assert state["topics"] == ("ШКАЛЫ",)


@pytest.mark.anyio
async def test_stream_skips_llm_when_budget_spent(monkeypatch):
    """Поток после исчерпания бюджета не начинается: сразу фрагменты и timeout."""

    async def _boom(*_args, **_kwargs):
        raise AssertionError("поток LLM не должен начинаться при исчерпанном бюджете")
        yield  # pragma: no cover — форма async-генератора

    monkeypatch.setattr(llm_router, "compose_answer_stream", _boom)
    factory = AgentFactory(client=None)
    monkeypatch.setattr(
        factory,
        "_state",
        lambda *a, **kw: {
            "query": QUERY,
            "history": [],
            "deadline_at": time.monotonic() - 1,
        },
    )
    monkeypatch.setattr(factory, "_retrieve", lambda state: {"chunks": CHUNKS})
    final = None
    async for item in factory.stream(QUERY):
        if "answer" in item:
            final = item["answer"]
    assert final is not None
    assert final["answer_kind"] == "fragments"
    assert final["llm_reason"] == "timeout"


def test_answer_body_puts_reasoning_effort_top_level(monkeypatch):
    """`reasoning_effort` — поле верхнего уровня: внутри chat_template_kwargs шлюз его не читает."""
    monkeypatch.setattr(settings, "assistant_llm_reasoning_effort", "none")
    body = llm_router._answer_body([], QUERY, [{"text": "т", "source_title": "s"}])
    assert body is not None
    assert body["reasoning_effort"] == "none"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_empty_reasoning_effort_is_not_sent(monkeypatch):
    """llama.cpp не знает reasoning_effort: пустое значение не отправляем вовсе."""
    monkeypatch.setattr(settings, "assistant_llm_reasoning_effort", "")
    payload = llm_router._thinking({})
    assert "reasoning_effort" not in payload
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_routing_payload_carries_reasoning_effort(monkeypatch):
    """Роутинг разделов тоже не должен «думать»: ключ верхнего уровня в теле тул-колла."""
    seen: list[dict] = []

    def _capture(payload, *, timeout=None):
        seen.append(payload)
        return {"choices": [{"message": {"tool_calls": []}}]}

    monkeypatch.setattr(settings, "assistant_llm_reasoning_effort", "none")
    monkeypatch.setattr(llm_router, "_request", _capture)
    llm_router.route_topics(QUERY)
    assert seen and seen[0]["reasoning_effort"] == "none"
    assert seen[0]["chat_template_kwargs"]["enable_thinking"] is False
