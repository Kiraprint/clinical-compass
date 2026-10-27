# Контракт: ответ ассистента пишет LLM по найденным сниппетам (FR-17), но
# детерминированный build_rag_answer остаётся fallback'ом и единственным путём
# для отказов/карт пациентов. Без сети: фейковый /chat/completions.
"""LLM-ответ по сниппетам: цитаты, границы и fallback (docs/03, docs/17)."""
from __future__ import annotations

import json
import urllib.request

import pytest

from compass.agent import REDLINE_EMPTY, REDLINE_TREATMENT, RAGChunk, build_rag_answer
from app.config import settings
from app.services import llm_router
from app.services.agent import AgentFactory

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
def _llm_off(monkeypatch):
    """Каждый тест стартует с выключенным LLM (CC_ASSISTANT_LLM_URL='')."""
    monkeypatch.setattr(settings, "assistant_llm_url", "")
    monkeypatch.setattr(settings, "assistant_llm_api_key", "")
    yield


def _enable(monkeypatch, url: str = "http://llm.test/v1") -> None:
    monkeypatch.setattr(settings, "assistant_llm_url", url)


class _FakeResp:
    def __init__(self, body: dict) -> None:
        self._body = body

    def read(self) -> bytes:
        return json.dumps(self._body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_chat(monkeypatch, reply: str, *, raise_exc: Exception | None = None) -> list[dict]:
    """Подменяет urlopen; возвращает тела ушедших запросов (в порядке вызовов)."""
    bodies: list[dict] = []

    def fake_urlopen(req, timeout=None):
        bodies.append({"url": req.full_url, "timeout": timeout, "body": json.loads(req.data)})
        if raise_exc is not None:
            raise raise_exc
        return _FakeResp({"choices": [{"message": {"role": "assistant", "content": reply}}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return bodies


def _no_network(monkeypatch) -> None:
    """LLM звать нельзя: любой HTTP — падение теста."""

    def boom(*args, **kwargs):
        raise AssertionError("LLM не должен вызываться на этом пути")

    monkeypatch.setattr(urllib.request, "urlopen", boom)


def _answer(monkeypatch, *, query: str = QUERY, chunks=None, history=None, **state) -> dict:
    """Прогон узла answer без Qdrant: только сборка ответа."""
    factory = AgentFactory(client=None)  # _answer не трогает Qdrant-клиент
    payload = {
        "query": query,
        "chunks": CHUNKS if chunks is None else chunks,
        "history": history or [],
        **state,
    }
    return factory._answer(payload)["answer"]


def test_compose_answer_sends_numbered_snippets_and_history(monkeypatch):
    """Сниппеты нумеруются с 1, история — в промпте, таймаут — свой (не роутера)."""
    _enable(monkeypatch)
    bodies = _fake_chat(monkeypatch, "Порог умеренной депрессии — 10 баллов [1].")
    history = [
        {"role": "CLINICIAN", "content": "Смотрим шкалу PHQ-9"},
        {"role": "ASSISTANT", "content": "PHQ-9 измеряет тяжесть депрессии [1]."},
    ]
    text = llm_router.compose_answer(
        history, QUERY, [{"text": c.text, "source_title": c.source_title} for c in CHUNKS]
    )
    assert text == "Порог умеренной депрессии — 10 баллов [1]."
    request = bodies[0]
    assert request["timeout"] == settings.assistant_llm_answer_timeout
    prompt = request["body"]["messages"][1]["content"]
    assert "[1] Гайдлайн PHQ-9:" in prompt
    assert "[2] Гайдлайн GAD-7:" in prompt
    assert "Врач: Смотрим шкалу PHQ-9" in prompt
    assert "Ассистент: PHQ-9 измеряет тяжесть депрессии [1]." in prompt
    system = request["body"]["messages"][0]
    assert system["role"] == "system"
    assert "Не ставь диагноз" in system["content"]
    assert "[n]" in system["content"]
    assert "max_tokens" not in request["body"]


def test_compose_answer_caps_history_like_rewrite(monkeypatch):
    """Старые реплики за бюджетом не уходят в модель (свежие важнее)."""
    _enable(monkeypatch)
    bodies = _fake_chat(monkeypatch, "ответ [1]")
    history = [{"role": "CLINICIAN", "content": f"реплика-{i} " + "x" * 900} for i in range(12)]
    assert llm_router.compose_answer(
        history, QUERY, [{"text": c.text, "source_title": c.source_title} for c in CHUNKS]
    )
    prompt = bodies[0]["body"]["messages"][1]["content"]
    assert "реплика-11" in prompt, "свежая реплика должна остаться"
    assert "реплика-0 " not in prompt, "самые старые реплики за бюджетом отбрасываются"
    assert prompt.count("реплика-") <= llm_router.REWRITE_HISTORY_MESSAGES


def test_answer_uses_llm_text_with_validated_citations(monkeypatch):
    """Валидная ссылка → текст модели и citations из настоящих чанков."""
    _enable(monkeypatch)
    _fake_chat(monkeypatch, "Порог 10 баллов соответствует умеренной депрессии [1].")
    answer = _answer(monkeypatch)
    assert answer["answer"].startswith("Порог 10 баллов")
    assert answer["grounded"] is True
    assert answer["refused"] is False
    assert answer["refusal_code"] is None
    assert [c["guideline_id"] for c in answer["citations"]] == ["who-mhgap-phq9"]
    assert answer["citations"][0]["source_title"] == "Гайдлайн PHQ-9"
    assert answer["topic"] is None


def test_unknown_citation_falls_back_to_deterministic(monkeypatch):
    """Ссылка [9] не указывает на фрагмент → детерминированный ответ."""
    _enable(monkeypatch)
    _fake_chat(monkeypatch, "Порог — 15 баллов [9].")
    answer = _answer(monkeypatch)
    expected = build_rag_answer(QUERY, CHUNKS)
    assert answer["answer"] == expected.answer
    assert [c["guideline_id"] for c in answer["citations"]] == [
        c.guideline_id for c in expected.citations
    ]


def test_citations_capped_and_deduped_by_guideline(monkeypatch):
    """Ссылки валидируются: ≤3 источников, без дублей, маркеры = порядок citations.

    [3] указывает на дубль gad7 → та же цитата под номером 2; [4] — четвёртый
    сниппет, после дедупа он третий. Видимые номера обязаны совпадать с
    порядком citations (его же показывает UI), иначе врач читает чужой источник.
    """
    _enable(monkeypatch)
    chunks = [
        *CHUNKS,
        RAGChunk(
            text="GAD-7: 5–9 лёгкая тревога.",
            source_title="Гайдлайн GAD-7 (продолжение)",
            guideline_id="who-mhgap-gad7",
            topic="ШКАЛЫ",
            score=0.6,
        ),
        RAGChunk(
            text="Триаж GREEN: состояние стабильно.",
            source_title="Триаж",
            guideline_id="triage-green",
            topic="ТРИАЖ",
            score=0.5,
        ),
    ]
    _fake_chat(monkeypatch, "Пороги: [1][2][3][4].")
    answer = _answer(monkeypatch, chunks=chunks)
    ids = [c["guideline_id"] for c in answer["citations"]]
    assert ids == ["who-mhgap-phq9", "who-mhgap-gad7", "triage-green"]
    assert len(ids) == len(set(ids)) <= 3
    assert answer["answer"] == "Пороги: [1][2][2][3]."


def test_citation_outside_prompt_window_falls_back(monkeypatch):
    """limit=20: в промпт уходят только первые 5 сниппетов → [6] — выдумка."""
    _enable(monkeypatch)
    chunks = [
        *CHUNKS,
        *[
            RAGChunk(
                text=f"Фрагмент {i}: критерий триажа.",
                source_title=f"Источник {i}",
                guideline_id=f"src-{i}",
                topic="ТРИАЖ",
                score=0.5 - i / 100,
            )
            for i in range(3, 8)
        ],
    ]
    payload = [{"text": c.text, "source_title": c.source_title} for c in chunks]
    assert llm_router.answer_snippet_window(payload) == llm_router.ANSWER_SNIPPETS
    _fake_chat(monkeypatch, "Критерий описан в рекомендациях [6].")
    answer = _answer(monkeypatch, chunks=chunks)
    expected = build_rag_answer(QUERY, chunks)
    assert answer["answer"] == expected.answer
    assert [c["guideline_id"] for c in answer["citations"]] == [
        c.guideline_id for c in expected.citations
    ]


def test_clinical_range_is_not_a_citation_and_visible_numbers_match_sources(monkeypatch):
    """«[3–4 баллов]» — диапазон шкалы, а не ссылка; видимые номера = citations."""
    _enable(monkeypatch)
    chunks = [
        *CHUNKS,
        RAGChunk(
            text="Шкала PHQ-9: 10–14 баллов — умеренная депрессия.",
            source_title="Гайдлайн PHQ-9 (интерпретация)",
            guideline_id="who-mhgap-phq9-scale",
            topic="ШКАЛЫ",
            score=0.5,
        ),
        RAGChunk(
            text="Триаж YELLOW: наблюдение участковым врачом.",
            source_title="Триаж",
            guideline_id="triage-yellow",
            topic="ТРИАЖ",
            score=0.4,
        ),
    ]
    _fake_chat(monkeypatch, "Умеренная депрессия — [3–4 баллов] по PHQ-9? Нет, 10–14 [3].")
    answer = _answer(monkeypatch, chunks=chunks)
    # [3] — третий сниппет (who-mhgap-phq9-scale), он же первый в citations.
    assert [c["guideline_id"] for c in answer["citations"]] == ["who-mhgap-phq9-scale"]
    assert answer["answer"] == (
        "Умеренная депрессия — [3–4 баллов] по PHQ-9? Нет, 10–14 [1]."
    )


def test_empty_reply_falls_back(monkeypatch):
    _enable(monkeypatch)
    _fake_chat(monkeypatch, "   ")
    answer = _answer(monkeypatch)
    assert answer["answer"] == build_rag_answer(QUERY, CHUNKS).answer


def test_timeout_falls_back(monkeypatch):
    """Таймаут LLM — не ошибка API: отвечает детерминированный путь."""
    _enable(monkeypatch)
    _fake_chat(monkeypatch, "не важно", raise_exc=TimeoutError("timed out"))
    answer = _answer(monkeypatch)
    assert answer["answer"] == build_rag_answer(QUERY, CHUNKS).answer
    assert answer["grounded"] is True


def test_http_error_falls_back(monkeypatch):
    _enable(monkeypatch)
    _fake_chat(monkeypatch, "не важно", raise_exc=urllib.error.URLError("connection refused"))
    answer = _answer(monkeypatch)
    assert answer["answer"] == build_rag_answer(QUERY, CHUNKS).answer


def test_redline_query_never_calls_llm(monkeypatch):
    """Диагноз/терапия — детерминированный отказ, текст в модель не уходит."""
    _enable(monkeypatch)
    _no_network(monkeypatch)
    answer = _answer(monkeypatch, query="Назначь Авиандр пациенту")
    assert answer["refused"] is True
    assert answer["refusal_code"] == REDLINE_TREATMENT


def test_no_chunks_never_calls_llm(monkeypatch):
    """Нечего цитировать — LLM не зовём (NO_KB_DATA детерминирован)."""
    _enable(monkeypatch)
    _no_network(monkeypatch)
    answer = _answer(monkeypatch, chunks=[])
    assert answer["refused"] is False  # NO_KB_DATA — не отказ, а «нет данных»
    assert answer["grounded"] is False
    assert answer["refusal_code"] == REDLINE_EMPTY


def test_patient_card_not_sent_to_llm(monkeypatch):
    """Карта пациента остаётся локальной: в промпте нет ни ФИО, ни patient_id."""
    _enable(monkeypatch)
    patient_id = "8f14e45f-1111-4a2b-9a3c-1234567890ab"
    ctx = {
        "patient_id": patient_id,
        "fio": "Иванов Иван Иванович",
        "prescribed_scale": "PHQ-9",
        "last_assessment": {"total_score": 17, "severity": "умеренная", "triage": "YELLOW",
                            "computed_at": "2026-10-20T09:00:00Z"},
        "trend": {"moving_avg": [17.0], "delta_7": 2, "delta_30": 4, "slope": 0.3},
        "open_escalations": [],
        "recent_diary": [],
    }
    bodies = _fake_chat(monkeypatch, "Порог 10 баллов [1].")
    answer = _answer(monkeypatch, patient_contexts=[ctx])
    assert answer["answer"] == "Порог 10 баллов [1]."
    sent = json.dumps(bodies[0]["body"], ensure_ascii=False)
    assert "Иванов" not in sent
    assert patient_id not in sent
    assert "Пациент" not in bodies[0]["body"]["messages"][1]["content"]
    # Цитаты — только реальные фрагменты КБ: карты в LLM-ответ не подмешиваются.
    assert [c["guideline_id"] for c in answer["citations"]] == ["who-mhgap-phq9"]


def test_disabled_llm_keeps_deterministic_answer(monkeypatch):
    """URL не задан → прежнее поведение без сети."""
    _no_network(monkeypatch)
    answer = _answer(monkeypatch)
    assert answer["answer"] == build_rag_answer(QUERY, CHUNKS).answer
    assert [c["guideline_id"] for c in answer["citations"]] == [
        "who-mhgap-phq9",
        "who-mhgap-gad7",
    ]


def test_compose_answer_returns_none_without_chunks_or_query(monkeypatch):
    _enable(monkeypatch)
    _no_network(monkeypatch)
    assert llm_router.compose_answer([], QUERY, []) is None
    assert llm_router.compose_answer([], "   ", [{"text": "текст", "source_title": "БЗ"}]) is None


def test_markers_beyond_citation_cap_are_dropped(monkeypatch):
    """4-й и 5-й источники отброшены лимитом → их маркеры не висят без ссылки."""
    _enable(monkeypatch)
    chunks = [
        *CHUNKS,
        *[
            RAGChunk(
                text=f"Критерий {i}.",
                source_title=f"Источник {i}",
                guideline_id=f"src-{i}",
                topic="ТРИАЖ",
                score=0.5,
            )
            for i in range(3, 6)
        ],
    ]
    _fake_chat(monkeypatch, "А [1] Б [2] В [3] Г [4] Д [5].")
    answer = _answer(monkeypatch, chunks=chunks)
    assert len(answer["citations"]) == 3
    assert answer["answer"] == "А [1] Б [2] В [3] Г Д."
