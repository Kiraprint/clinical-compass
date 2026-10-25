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


def _fake_chat_multi(monkeypatch, names: list[str]):
    """Фейковый /chat/completions, где модель вызвала несколько тулз."""
    import urllib.request

    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _FakeResp({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"type": "function", "function": {"name": n, "arguments": "{}"}}
                        for n in names
                    ],
                }
            }]
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


def test_router_selects_two_topics_for_drug_evidence_question(monkeypatch):
    """«Препарат + доказательства» → две темы: ЛЕКАРСТВА и НАУЧНЫЕ-СТАТЬИ (FR-14)."""
    from app.services import llm_router

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    _fake_chat_multi(monkeypatch, ["search_lekarstva", "search_nauchnye_stati"])
    assert llm_router.route_topics("семаглутид и риск депрессии: что показывают обзоры") == (
        "ЛЕКАРСТВА",
        "НАУЧНЫЕ-СТАТЬИ",
    )
    # route_topic остаётся совместимой обёрткой над первой темой
    assert llm_router.route_topic("семаглутид и риск депрессии") == "ЛЕКАРСТВА"


def test_router_caps_topics_and_skips_unknown_tools(monkeypatch):
    """Не больше двух тем; неизвестная тулза пропускается, дубликаты схлопываются."""
    from app.services import llm_router

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    _fake_chat_multi(monkeypatch, [
        "search_skaly", "search_unknown", "search_skaly", "search_triazh", "search_lekarstva",
    ])
    assert llm_router.route_topics("вопрос") == ("ШКАЛЫ", "ТРИАЖ")


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


# --- Переформулировка уточняющего вопроса (FR-21) -----------------------------

def _fake_rewrite(monkeypatch, reply: str | None, *, raise_exc: Exception | None = None):
    """Фейковый /chat/completions для rewrite_followup: только content, без тулз."""
    import urllib.request

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm.test/v1")
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        if raise_exc is not None:
            raise raise_exc
        return _FakeResp({"choices": [{"message": {"role": "assistant", "content": reply}}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured

_HISTORY = [
    {"role": "CLINICIAN", "content": "Сертралин при депрессии — что известно?"},
    {"role": "ASSISTANT", "content": "В базе есть инструкция по сертралину."},
]

def test_rewrite_returns_standalone_question(monkeypatch):
    from app.services import llm_router

    captured = _fake_rewrite(monkeypatch, "Какие дозы сертралина при депрессии?")
    assert llm_router.rewrite_followup(_HISTORY, "а в каких дозах?") == (
        "Какие дозы сертралина при депрессии?"
    )
    body = captured["body"]
    assert body["temperature"] == 0.0
    assert "tools" not in body
    prompt = body["messages"][1]["content"]
    assert "Сертралин при депрессии" in prompt and "а в каких дозах?" in prompt

def test_rewrite_cleans_quotes_and_multiline_reply(monkeypatch):
    """Модель ответила абзацем в кавычках → берём первую строку без обёрток."""
    from app.services import llm_router

    _fake_rewrite(monkeypatch, '  «Вопрос: какие дозы сертралина?»\nПояснение: ...\n')
    assert llm_router.rewrite_followup(_HISTORY, "а в каких дозах?") == (
        "какие дозы сертралина?"
    )

def test_rewrite_off_when_no_url(monkeypatch):
    """LLM выключен → None без сетевого вызова."""
    import urllib.request

    from app.services import llm_router

    def _no_network(*args, **kwargs):
        raise AssertionError("LLM выключен — вызова быть не должно")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    assert llm_router.rewrite_followup(_HISTORY, "а в каких дозах?") is None

def test_rewrite_skipped_without_history(monkeypatch):
    """Одиночный вопрос не тратит LLM-вызов: переформулировать нечего."""
    import urllib.request

    from app.services import llm_router

    def _no_network(*args, **kwargs):
        raise AssertionError("нет истории — вызова быть не должно")

    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm.test/v1")
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    assert llm_router.rewrite_followup([], "а в каких дозах?") is None
    assert llm_router.rewrite_followup([{"role": "CLINICIAN", "content": "  "}], "вопрос") is None

@pytest.mark.parametrize(
    "reply,raise_exc",
    [
        ("", None),
        ("   \n\t ", None),
        (None, "URLError"),
        (None, "TimeoutError"),
        (None, "KeyError"),
    ],
)
def test_rewrite_falls_back_on_empty_or_error(monkeypatch, reply, raise_exc):
    import urllib.error

    from app.services import llm_router

    exc = None
    if raise_exc == "URLError":
        exc = urllib.error.URLError("down")
    elif raise_exc == "TimeoutError":
        exc = TimeoutError("timeout")
    elif raise_exc == "KeyError":
        exc = KeyError("choices")
    _fake_rewrite(monkeypatch, reply, raise_exc=exc)
    assert llm_router.rewrite_followup(_HISTORY, "а в каких дозах?") is None

def test_rewrite_clips_long_history_from_the_end(monkeypatch):
    """Бюджет: 8 последних реплик, по 800 символов, всего 3000 (свежее важнее)."""
    from app.services import llm_router

    history = [
        {"role": "CLINICIAN", "content": f"msg{i:02d} " + "я" * 1000} for i in range(20)
    ]
    captured = _fake_rewrite(monkeypatch, "вопрос")
    assert llm_router.rewrite_followup(history, "а в каких дозах?") == "вопрос"
    prompt = captured["body"]["messages"][1]["content"]
    lines = [ln for ln in prompt.splitlines() if ln.startswith(("Врач:", "Ассистент:"))]
    assert len(lines) <= llm_router.REWRITE_HISTORY_MESSAGES
    assert all(len(ln) <= llm_router.REWRITE_MESSAGE_CHARS + len("Врач: ") for ln in lines)
    body_block = "\n".join(lines)
    assert len(body_block) <= llm_router.REWRITE_HISTORY_CHARS
    assert "msg19" in body_block  # свежая реплика
    assert "msg00" not in body_block  # старая отброшена

def test_rewrite_clips_long_answer(monkeypatch):
    from app.services import llm_router

    _fake_rewrite(monkeypatch, "вопрос " + "я" * 5000)
    out = llm_router.rewrite_followup(_HISTORY, "а в каких дозах?")
    assert out is not None and len(out) == llm_router.REWRITE_QUERY_CHARS
