# Контракт: LLM-роутер раздела БЗ (docs/04 FR-14, docs/08). Без сети: фейковый
# /chat/completions через подмену urllib. Тесты — docs/06 §3.
"""LLM-роутер топика: тулколлинг выбора раздела, fallback при недоступности."""
from __future__ import annotations

import json

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _llm_url(monkeypatch):
    """Каждый тест стартует с выключенным роутером (CC_ASSISTANT_LLM_URL='')."""
    monkeypatch.setattr(settings, "assistant_llm_url", "")
    monkeypatch.setattr(settings, "assistant_llm_api_key", "")
    yield


def _fake_chat(monkeypatch, tool_name: str | None, *, raise_exc: Exception | None = None):
    """Подменяет urllib.request.urlopen на фейковый /chat/completions.

    tool_name=None → модель не вызвала тулзу (вне разделов).
    raise_exc задаётся для проверки fallback'а (таймаут/URLError).
    """
    import urllib.request

    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        captured["headers"] = dict(req.headers)
        if raise_exc is not None:
            raise raise_exc
        if tool_name is None:
            return _FakeResp({"choices": [{"message": {"role": "assistant", "content": "вне разделов"}}]})
        return _FakeResp({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "type": "function",
                        "function": {"name": tool_name, "arguments": "{}"},
                    }],
                }
            }]
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


class _FakeResp:
    def __init__(self, body: dict):
        self._body = body

    def read(self) -> bytes:
        return json.dumps(self._body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_router_off_when_no_url(monkeypatch):
    """CC_ASSISTANT_LLM_URL пуст → роутер выключен, topic=None без сетевых вызовов."""
    from app.services import llm_router

    import urllib.request

    def boom(*a, **k):
        raise AssertionError("urlopen не должен вызываться")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert llm_router.route_topic("какие шкалы для ГТР?") is None


def test_router_selects_topic_via_tool_call(monkeypatch):
    """LLM вернул tool_call search_lekarstva → topic ЛЕКАРСТВА."""
    from app.services import llm_router

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    captured = _fake_chat(monkeypatch, "search_lekarstva")
    assert llm_router.route_topic("дозировка Авиандра?") == "ЛЕКАРСТВА"
    # в тело ушли tools (все разделы корпуса), запрос — очищенный текст
    assert "tools" in captured["body"]
    names = {t["function"]["name"] for t in captured["body"]["tools"]}
    assert names == {
        "search_skaly",
        "search_triazh",
        "search_rekomendacii",
        "search_lekarstva",
        "search_nauchnye_stati",
    }


@pytest.mark.parametrize("tool_name,expected", [
    ("search_skaly", "ШКАЛЫ"),
    ("search_triazh", "ТРИАЖ"),
    ("search_rekomendacii", "РЕКОМЕНДАЦИИ"),
    ("search_lekarstva", "ЛЕКАРСТВА"),
    ("search_nauchnye_stati", "НАУЧНЫЕ-СТАТЬИ"),
])
def test_router_topic_mapping(monkeypatch, tool_name, expected):
    from app.services import llm_router

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    _fake_chat(monkeypatch, tool_name)
    assert llm_router.route_topic("вопрос") == expected


def test_router_no_tool_call_means_all(monkeypatch):
    """Модель не вызвала тулзу (вопрос вне разделов) → None = все разделы."""
    from app.services import llm_router

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    _fake_chat(monkeypatch, None)
    assert llm_router.route_topic("привет, как дела?") is None


def test_router_falls_back_on_http_error(monkeypatch):
    """Ошибка сети/HTTP → None (поиск по всем), без падения."""
    from app.services import llm_router

    import urllib.error

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    _fake_chat(monkeypatch, "search_skaly",
               raise_exc=urllib.error.URLError("connection refused"))
    assert llm_router.route_topic("вопрос") is None


def test_router_falls_back_on_timeout(monkeypatch):
    from app.services import llm_router

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    _fake_chat(monkeypatch, "search_skaly", raise_exc=TimeoutError("timeout"))
    assert llm_router.route_topic("вопрос") is None


def test_router_falls_back_on_unknown_tool(monkeypatch):
    """Неизвестное имя тулзы → лог + None (не падаем, не фильтруем)."""
    from app.services import llm_router

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    _fake_chat(monkeypatch, "search_magic")
    assert llm_router.route_topic("вопрос") is None


def test_router_sends_bearer_token_when_key_set(monkeypatch):
    """Шлюз с ключом (omniroute): Authorization: Bearer из CC_ASSISTANT_LLM_API_KEY."""
    from app.services import llm_router

    monkeypatch.setattr(settings, "assistant_llm_url", "http://omniroute:20128/v1")
    monkeypatch.setattr(settings, "assistant_llm_api_key", "sk-test")
    captured = _fake_chat(monkeypatch, "search_skaly")
    assert llm_router.route_topic("шкала PHQ-9") == "ШКАЛЫ"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_router_omits_auth_header_without_key(monkeypatch):
    """llama-server без ключа: заголовок не отправляется вовсе."""
    from app.services import llm_router

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    captured = _fake_chat(monkeypatch, "search_skaly")
    assert llm_router.route_topic("шкала PHQ-9") == "ШКАЛЫ"
    assert "Authorization" not in captured["headers"]


def test_router_does_not_cap_output_tokens(monkeypatch):
    """Лимит вывода не передаём: thinking съедает бюджет, tool_call не влезает."""
    from app.services import llm_router

    monkeypatch.setattr(settings, "assistant_llm_url", "http://omniroute:20128/v1")
    captured = _fake_chat(monkeypatch, "search_skaly")
    assert llm_router.route_topic("шкала PHQ-9") == "ШКАЛЫ"
    assert "max_tokens" not in captured["body"]
