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
    # [2] и [3] — два чанка одного гайдлайна: citation одна, значит и маркер
    # один (`_collapse_marker_repeats`), а не «[2][2]».
    assert answer["answer"] == "Пороги: [1][2][3]."


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


PATIENT_ID = "8f14e45f-1111-4a2b-9a3c-1234567890ab"
# Как patient_context() ВЫГЛЯДИТ до скраба: сырое ФИО + контакты, которых в
# промпт быть не должно. Карта фикстуры спецкатегории ПДн (docs/07 §7).
CARD = {
    "patient_id": PATIENT_ID,
    "fio": "Иванов Иван Иванович",
    "age": 34,
    "phone": "+79991234567",
    "prescribed_scales": ["PHQ-9", "GAD-7"],
    "last_assessment": {
        "total_score": 17,
        "severity": "умеренная",
        "triage": "YELLOW",
        "computed_at": "2026-10-20T09:00:00Z",
    },
    "trend": {"moving_avg": [17.0], "delta_7": 2, "delta_30": 4, "slope": 0.3},
    "open_escalations": [{"kind": "SUICIDE_RISK"}],
    "recent_diary": [{"content": "[ПАЦИЕНТ]: к вечеру тревога и бессонница"}],
}


def test_patient_card_sent_anonymized_with_scores_and_trend(monkeypatch):
    """Карта уходит в модель блоком «Карта пациента (обезлично)» без ПДн.

    Реальное тело запроса к LLM (перехват urlopen): шкалы, последняя оценка,
    триаж, тренд, эскалации и дневник — есть; ФИО, телефон, patient_id — нет,
    вместо ФИО стоит маска [ПАЦИЕНТ].
    """
    _enable(monkeypatch)
    bodies = _fake_chat(monkeypatch, "Умеренная депрессия, балл 17 [П1]. Порог 10 [1].")
    answer = _answer(monkeypatch, patient_contexts=[CARD])

    prompt = bodies[0]["body"]["messages"][1]["content"]
    assert "Карта пациента (обезлично):" in prompt
    assert "П1: пациент: [ПАЦИЕНТ]" in prompt
    assert "возраст: 34" in prompt
    assert "шкалы: PHQ-9, GAD-7" in prompt
    assert "последняя оценка: 17 (умеренная) от 2026-10-20" in prompt
    assert "триаж: YELLOW" in prompt
    assert "тренд: среднее 17.0, дельта 7д 2, дельта 30д 4, наклон 0.3" in prompt
    assert "открытые эскалации: 1 (SUICIDE_RISK)" in prompt
    assert "дневник: [ПАЦИЕНТ]: к вечеру тревога и бессонница" in prompt
    # Системный промпт объясняет модель, как ссылаться на карту.
    system = bodies[0]["body"]["messages"][0]["content"]
    assert "[П1]" in system and "Карта пациента" in system
    # ПДн и внутренний id в запрос не попадают ни в каком виде.
    sent = json.dumps(bodies[0]["body"], ensure_ascii=False)
    for pii in ("Иванов", "Иван", "+79991234567", PATIENT_ID):
        assert pii not in sent
    # Сниппетная ссылка [1] остаётся сниппетной, [П1] добавляет карту в конец.
    assert [c["guideline_id"] for c in answer["citations"]] == [
        "who-mhgap-phq9",
        f"patient:{PATIENT_ID}",
    ]


def test_patient_reference_becomes_patient_citation(monkeypatch):
    """Ссылка [П1] даёт citation topic=ПАЦИЕНТ при answer_kind="llm"."""
    _enable(monkeypatch)
    _fake_chat(monkeypatch, "Балл 17 — умеренная депрессия [П1]; порог 10 [1].")
    answer = _answer(monkeypatch, patient_contexts=[CARD])

    assert answer["answer_kind"] == "llm"
    assert answer["answer"] == "Балл 17 — умеренная депрессия [П1]; порог 10 [1]."
    assert [c["guideline_id"] for c in answer["citations"]] == [
        "who-mhgap-phq9",
        f"patient:{PATIENT_ID}",
    ]
    card = answer["citations"][1]
    assert card["topic"] == "ПАЦИЕНТ"
    assert card["source_title"] == "Карта пациента"
    assert answer["fragments"] == []  # карта — не фрагмент КБ


def test_patient_only_answer_without_snippets_falls_back(monkeypatch):
    """Карта без сниппетов до модели не доходит: детерминированный ответ."""
    _enable(monkeypatch)
    _no_network(monkeypatch)
    answer = _answer(monkeypatch, chunks=[], patient_contexts=[CARD])

    assert answer["answer_kind"] == "fragments"
    assert [c["guideline_id"] for c in answer["citations"]] == [f"patient:{PATIENT_ID}"]
    assert answer["citations"][0]["topic"] == "ПАЦИЕНТ"


def test_body_without_patients_is_unchanged(monkeypatch):
    """Без пациентов тело запроса прежнее: ни блока карты, ни лишних ключей."""
    _enable(monkeypatch)
    bodies = _fake_chat(monkeypatch, "Порог 10 баллов [1].")
    payload = [{"text": c.text, "source_title": c.source_title} for c in CHUNKS]
    llm_router.compose_answer([], QUERY, payload)

    sent = bodies[0]["body"]
    # Снимок тела: пользовательское сообщение — ровно вопрос и нумерованные
    # сниппеты, без «Карта пациента»; набор ключей тела не расширился.
    assert sent["messages"][1]["content"] == (
        f"Вопрос врача: {QUERY}\n\n"
        "Сниппеты базы знаний:\n"
        f"[1] Гайдлайн PHQ-9: {CHUNKS[0].text}\n"
        f"[2] Гайдлайн GAD-7: {CHUNKS[1].text}"
    )
    assert "Карта пациента" not in sent["messages"][1]["content"]


def test_patient_reference_outside_sent_cards_gives_no_citation(monkeypatch):
    """[П2] при одной карте — выдумка: citation нет, маркер вырезан."""
    _enable(monkeypatch)
    _fake_chat(monkeypatch, "Балл 17 [П2]; порог 10 [1].")
    answer = _answer(monkeypatch, patient_contexts=[CARD])

    assert [c["guideline_id"] for c in answer["citations"]] == ["who-mhgap-phq9"]
    assert answer["answer"] == "Балл 17; порог 10 [1]."


def test_unresolvable_patient_marker_stripped_without_cards(monkeypatch):
    """[П9] без карт вообще — маркер вырезан так же, как [n] вне окна."""
    _enable(monkeypatch)
    _fake_chat(monkeypatch, "Балл 17 [П9]; порог 10 [1].")
    answer = _answer(monkeypatch, patient_contexts=None)

    assert answer["answer"] == "Балл 17; порог 10 [1]."
    assert [c["guideline_id"] for c in answer["citations"]] == ["who-mhgap-phq9"]


def test_singular_patient_context_is_scrubbed_in_guard(monkeypatch):
    """Сырая карта из patient_context: дневник скрабится в guard'е, не в LLM."""
    _enable(monkeypatch)
    bodies = _fake_chat(monkeypatch, "Балл 17 [П1]; порог 10 [1].")
    card = {
        **CARD,
        "fio": "Иванов Иван Иванович",
        "recent_diary": [
            {"id": "d-1", "content": "звонил +79991234567, просил перезвонить"}
        ],
    }
    answer = _answer(monkeypatch, patient_context=card)

    prompt = bodies[0]["body"]["messages"][1]["content"]
    assert "П1: пациент: [ПАЦИЕНТ]" in prompt
    assert "+79991234567" not in prompt
    assert "[ТЕЛЕФОН]" in prompt
    assert "Иванов" not in prompt
    assert [c["guideline_id"] for c in answer["citations"]] == [
        "who-mhgap-phq9",
        f"patient:{PATIENT_ID}",
    ]


def test_duplicate_patient_context_is_deduped(monkeypatch):
    """Одна карта в patient_context и patient_contexts — одна П1 и одна citation."""
    _enable(monkeypatch)
    bodies = _fake_chat(monkeypatch, "Балл 17 [П1]; порог 10 [1].")
    answer = _answer(
        monkeypatch, patient_context=CARD, patient_contexts=[dict(CARD)]
    )

    prompt = bodies[0]["body"]["messages"][1]["content"]
    assert "П1: пациент:" in prompt
    assert "П2: пациент:" not in prompt
    assert [c["guideline_id"] for c in answer["citations"]] == [
        "who-mhgap-phq9",
        f"patient:{PATIENT_ID}",
    ]


def test_patient_cards_capped_by_answer_patients(monkeypatch):
    """Карт в промпте не больше ANSWER_PATIENTS; 4-я невидима — citation нет."""
    _enable(monkeypatch)
    cards = [
        {**CARD, "patient_id": f"p-{i}", "fio": "[ПАЦИЕНТ]"} for i in range(1, 5)
    ]
    bodies = _fake_chat(monkeypatch, "Четвёртая карта [П4]; порог 10 [1].")
    answer = _answer(monkeypatch, patient_contexts=cards)

    prompt = bodies[0]["body"]["messages"][1]["content"]
    assert "П3: пациент:" in prompt
    assert "П4: пациент:" not in prompt
    assert "patient:p-4" not in [c["guideline_id"] for c in answer["citations"]]
    assert [c["guideline_id"] for c in answer["citations"]] == ["who-mhgap-phq9"]


def test_flag_off_keeps_patient_card_out_of_prompt(monkeypatch):
    """CC_ASSISTANT_LLM_PATIENT_CONTEXT=false → прежнее поведение и цитаты."""
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "assistant_llm_patient_context", False)
    bodies = _fake_chat(monkeypatch, "Балл 17 — умеренная депрессия [П1]; порог 10 [1].")
    answer = _answer(monkeypatch, patient_contexts=[CARD])

    prompt = bodies[0]["body"]["messages"][1]["content"]
    assert "Карта пациента (обезлично):" not in prompt
    assert "[ПАЦИЕНТ]" not in prompt
    # Ссылка на невидимую карту — выдумка: citation по ней не собирается.
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


def test_duplicate_markers_collapse_to_one(monkeypatch):
    """`[2][2]` склеивается: citation по гайдлайну одна, повтор читался как «№2, №2»."""
    _enable(monkeypatch)
    _fake_chat(monkeypatch, "Порог 10 баллов [1][1], тяжёлая тревога [2].")

    answer = _answer(monkeypatch)

    assert answer["answer"] == "Порог 10 баллов [1], тяжёлая тревога [2]."


def test_markers_collapse_after_renumbering(monkeypatch):
    """[1][2] по двум чанкам одного гайдлайна → одна citation, значит один маркер.

    `_renumber_citations` переводит второй индекс на ту же citation, и в тексте
    остаётся `[1][1]`; склейка обязана случиться и после перенумерации.
    """
    _enable(monkeypatch)
    twin = RAGChunk(
        text="PHQ-9: 0–4 нет депрессии.",
        source_title="Гайдлайн PHQ-9 (копия)",
        guideline_id=CHUNKS[0].guideline_id,
        topic="ШКАЛЫ",
        score=0.6,
    )
    _fake_chat(monkeypatch, "Порог 0–4 — нет депрессии [1][2].")

    answer = _answer(monkeypatch, chunks=[CHUNKS[0], twin])

    assert len(answer["citations"]) == 1
    assert answer["answer"] == "Порог 0–4 — нет депрессии [1]."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[П 1] [П 1]", "[П1]"),
        ("[ 2 ][ 2 ]", "[2]"),
        ("[П 1][П1]", "[П1]"),
        ("[1][П1]", "[1][П1]"),
        ("[1][2]", "[1][2]"),
        ("[1] и [1]", "[1] и [1]"),
        ("[2024] — год", "[2024] — год"),
    ],
)
def test_marker_repeat_normalization(raw, expected):
    """Пробелы внутри маркера не мешают склейке, разные виды — не склеиваются."""
    from app.services.agent import _collapse_marker_repeats

    assert _collapse_marker_repeats(raw) == expected


def test_patient_markers_collapse_in_answer(monkeypatch):
    """`[ П1 ] [П1 ]` в ответе модели: один маркер и одна citation карты.

    Карта проходит `_strip_patient_citations`/`_cited_patients` по тем же
    пробельным правилам, что и склейка, иначе маркер нормализуется, но
    перестаёт считаться ссылкой на карту (и ответ уходит в фолбэк).
    """
    _enable(monkeypatch)
    _fake_chat(monkeypatch, "Шкалы PHQ-9 и HADS [ П1 ] [П1 ].")

    answer = _answer(
        monkeypatch,
        patient_contexts=[{"patient_id": "p-1", "fio": "[ПАЦИЕНТ]", "age": 34}],
    )

    assert answer["answer"] == "Шкалы PHQ-9 и HADS [П1]."
    patients = [c for c in answer["citations"] if c["topic"] == "ПАЦИЕНТ"]
    assert len(patients) == 1 and patients[0]["guideline_id"] == "patient:p-1"
