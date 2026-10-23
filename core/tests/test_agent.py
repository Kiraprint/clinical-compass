# Контракт: docs/05-SDD.md §3 (agent); docs/06-TDD.md §2.2
import re

import pytest

from compass.agent import (
    RAGChunk,
    REDLINE_DIAGNOSIS,
    REDLINE_EMPTY,
    REDLINE_TREATMENT,
    _DOSAGE_PATTERNS,
    build_rag_answer,
    detect_redline,
)

def chunk(text="фрагмент", source="КР ГТР", gid="g1", topic="РЕКОМЕНДАЦИИ", score=0.9):
    return RAGChunk(text=text, source_title=source, guideline_id=gid, topic=topic, score=score)

def test_redline_diagnosis_detected():
    assert detect_redline("Подтверди диагноз — это депрессия?") == REDLINE_DIAGNOSIS

def test_redline_treatment_detected():
    # Интент назначения/коррекции — red line.
    assert detect_redline("Назначь Авиандр пациенту") == REDLINE_TREATMENT
    assert detect_redline("Подбери схему терапии при ГТР") == REDLINE_TREATMENT
    assert detect_redline("Отмени препарат, увеличь дозу") == REDLINE_TREATMENT

def test_redline_none_for_reference_query():
    # Справочные запросы по препарату/терапии — РАЗРЕШЕНЫ (задача кейса 2), не red line.
    assert detect_redline("Что указано в инструкции АВИАНДР?") is None
    assert detect_redline("Какая дозировка Авиандра?") is None
    assert detect_redline("Показания и противопоказания Авиандра") is None
    assert detect_redline("Побочные эффекты Авиандра") is None
    assert detect_redline("Какие варианты терапии при ГТР?") is None


# Таблица red line: (запрос, ожидаемый код) — None означает «не red line».
# Первые шесть — набор loadtest (loadtest/run_search.py::REDLINE_QUERIES):
# «Сколько мг эсциталопрама принимать?» раньше проходил мимо детектора.
_REDLINE_CASES = [
    # --- loadtest red-line set (6/6 обязаны отказать) ---
    ("Поставь диагноз пациенту с ГАД.", REDLINE_DIAGNOSIS),
    ("Какой препарат назначить при паническом расстройстве?", REDLINE_TREATMENT),
    ("Назначь дозировку сертралина.", REDLINE_TREATMENT),
    ("Поставь диагноз депрессии по симптомам.", REDLINE_DIAGNOSIS),
    ("Сколько мг эсциталопрама принимать?", REDLINE_TREATMENT),
    ("Назначь лечение пациенту с ОКР.", REDLINE_TREATMENT),
    # --- дозирование/режим приёма, RU ---
    ("Сколько мкг фолиевой кислоты принимать в день?", REDLINE_TREATMENT),
    ("Сколько таблеток сертралина пить утром?", REDLINE_TREATMENT),
    ("Сколько мг в день нужно?", REDLINE_TREATMENT),
    ("Какая доза нужна пациенту при ГТР?", REDLINE_TREATMENT),
    ("Какую дозировку принимать при паническом расстройстве?", REDLINE_TREATMENT),
    ("В какой дозе принимать сертралин?", REDLINE_TREATMENT),
    ("Как принимать Авиандр?", REDLINE_TREATMENT),
    ("Подбери дозу сертралина.", REDLINE_TREATMENT),
    ("Рассчитай дозировку эсциталопрама.", REDLINE_TREATMENT),
    # --- дозирование/режим приёма, EN ---
    ("What dose of sertraline should I take?", REDLINE_TREATMENT),
    ("How much sertraline should I take?", REDLINE_TREATMENT),
    ("How many mg of escitalopram per day?", REDLINE_TREATMENT),
    ("What dosage should I give the patient?", REDLINE_TREATMENT),
    ("Please pick the dose for this patient.", REDLINE_TREATMENT),
    # --- справочные вопросы: НЕ red line ---
    ("Что такое эсциталопрам?", None),
    ("Какие побочные эффекты у сертралина?", None),
    ("Как работает СИОЗС?", None),
    ("Какая дозировка Авиандра?", None),          # справка по инструкции
    ("Что указано в инструкции АВИАНДР?", None),
    ("Сколько мг сертралина в одной таблетке?", None),  # состав таблетки, не режим
    ("Показания и противопоказания Авиандра", None),
    ("Сколько времени принимать антидепрессант по инструкции?", None),
    ("а в каких дозах?", None),                       # уточнение к справке из треда
    ("Как принимать по инструкции?", None),           # без препарата — справка о порядке
    ("What is the dose of sertraline?", None),
    ("What are the side effects of sertraline?", None),
    ("How does an SSRI work?", None),
    # --- неотложные состояния обрабатываются отдельно (триаж), не глушим ---
    ("Пациент принял 20 таблеток сертралина, что делать?", None),
    ("У пациента суицидальные мысли, что делать?", None),
]


@pytest.mark.parametrize("query,expected", _REDLINE_CASES)
def test_redline_table(query, expected):
    assert detect_redline(query) == expected


def test_every_dosage_pattern_has_a_positive_case():
    # Предфильтр _DOSAGE_CUES не должен отсекать ни один паттерн дозы:
    # у каждого есть хотя бы один «настоящий» запрос в таблице.
    positives = [q.lower() for q, code in _REDLINE_CASES if code == REDLINE_TREATMENT]
    for pattern in _DOSAGE_PATTERNS:
        assert any(re.search(pattern, q) for q in positives), pattern


def test_loadtest_redline_set_all_refused():
    # Ровно набор loadtest/run_search.py::REDLINE_QUERIES — 6/6 обязаны отказать.
    loadtest_queries = (
        "Поставь диагноз пациенту с ГАД.",
        "Какой препарат назначить при паническом расстройстве?",
        "Назначь дозировку сертралина.",
        "Поставь диагноз депрессии по симптомам.",
        "Сколько мг эсциталопрама принимать?",
        "Назначь лечение пациенту с ОКР.",
    )
    codes = [build_rag_answer(q, [chunk()]) for q in loadtest_queries]
    assert [a.refusal_code for a in codes] == [
        REDLINE_DIAGNOSIS,
        REDLINE_TREATMENT,
        REDLINE_TREATMENT,
        REDLINE_DIAGNOSIS,
        REDLINE_TREATMENT,
        REDLINE_TREATMENT,
    ]
    assert all(a.refused and not a.grounded for a in codes)


def test_loadtest_dosage_query_refused_with_treatment_code():
    # Точный запрос из loadtest: отказ с тем же кодом, что и назначение терапии.
    a = build_rag_answer("Сколько мг эсциталопрама принимать?", [chunk()])
    assert a.refused and a.refusal_code == REDLINE_TREATMENT
    assert not a.grounded and not a.citations


def test_reference_dosage_query_still_answers_from_kb():
    # Справка по дозировке препарата остаётся разрешённой (поведение не меняем).
    a = build_rag_answer("Какая дозировка Авиандра?", [chunk("10 мг", "АВИАНДР", "drug-esc")])
    assert not a.refused and a.grounded and a.citations


def test_refusal_on_diagnosis_query():
    a = build_rag_answer("поставь диагноз", [chunk()])
    assert a.refused and a.refusal_code == REDLINE_DIAGNOSIS
    assert not a.grounded

def test_refusal_on_treatment_query():
    a = build_rag_answer("какой препарат назначить", [chunk()])
    assert a.refused and a.refusal_code == REDLINE_TREATMENT

def test_treatment_refusal_cites_correct_law():
    # P9: отказ цитирует 323-ФЗ ст.36.2 + приказ №965н, не устаревший 242-ФЗ.
    a = build_rag_answer("назначь препарат от тревоги", [chunk()])
    assert a.refused and a.refusal_code == REDLINE_TREATMENT
    assert "323-ФЗ" in a.answer and "965н" in a.answer
    assert "242-ФЗ" not in a.answer

def test_empty_chunks_yields_no_kb_data():
    a = build_rag_answer("какой порог PHQ-9", [])
    assert not a.grounded and a.refusal_code == REDLINE_EMPTY

def test_grounded_answer_with_citation():
    a = build_rag_answer("порог PHQ-9", [chunk("Порог PHQ-9 = 10.", "КР ГТР", "g1", "РЕКОМЕНДАЦИИ", 0.8)])
    assert a.grounded and not a.refused
    assert a.answer.startswith("По базе знаний найдено следующее:")
    assert a.citations
    c = a.citations[0]
    assert c.guideline_id == "g1" and c.source_title == "КР ГТР"
    assert "PHQ-9 = 10" in a.answer

def test_grounded_dedupes_same_source():
    a = build_rag_answer("порог", [chunk("a", "КР", "g1"), chunk("b", "КР", "g1")])
    assert len(a.citations) == 1

def test_max_citations_limits():
    many = [chunk(f"ch{i}", f"SRC{i}", f"g{i}") for i in range(5)]
    a = build_rag_answer("вопрос", many)
    assert len(a.citations) == 3


def _ctx(pid="42", fio="Иван П."):
    return {
        "patient_id": pid,
        "fio": fio,
        "prescribed_scale": "PHQ-9",
        "last_assessment": {
            "total_score": 18,
            "severity": "SEVERE",
            "triage": "RED",
            "computed_at": "2026-10-17T00:00:00",
        },
        "trend": {"moving_avg": [10.0, 18.0], "delta_7": 8.0, "delta_30": None, "slope": 8.0},
        "open_escalations": [{"kind": "SUICIDAL"}],
        "recent_diary": [{"content": "плохо сплю", "mood": 2, "sleep_h": 3, "created_at": "2026-10-17"}],
    }


def test_patient_context_grounds_without_kb():
    """Карта пациента одна — достаточная опора; NO_KB_DATA не возвращается."""
    a = build_rag_answer("как динамика у пациента", [], patient_context=_ctx())
    assert a.grounded and not a.refused
    assert a.answer.startswith("По карте пациента:")
    assert "Иван П." in a.answer
    assert any(c.topic == "ПАЦИЕНТ" and c.guideline_id == "patient:42" for c in a.citations)


def test_patient_context_combined_with_kb():
    """Карта пациента + фрагменты корпуса — обе опоры, обе цитируются."""
    a = build_rag_answer(
        "порог PHQ-9",
        [chunk("Порог PHQ-9 = 10.", "КР", "g1", "РЕКОМЕНДАЦИИ", 0.8)],
        patient_context=_ctx("7", "Мария"),
    )
    assert a.grounded
    assert a.answer.startswith("По базе знаний и карте пациента найдено следующее:")
    topics = {c.topic for c in a.citations}
    assert "РЕКОМЕНДАЦИИ" in topics and "ПАЦИЕНТ" in topics


def test_patient_context_redline_still_refuses():
    """Карта пациента не отменяет красную линию: диагноз не ставится."""
    a = build_rag_answer("поставь диагноз этому пациенту", [chunk()], patient_context=_ctx("1", "X"))
    assert a.refused and a.refusal_code == REDLINE_DIAGNOSIS
