# Контракт: docs/05-SDD.md §3 (agent); docs/06-TDD.md §2.2
from compass.agent import (
    RAGChunk,
    REDLINE_DIAGNOSIS,
    REDLINE_EMPTY,
    REDLINE_TREATMENT,
    build_rag_answer,
    detect_redline,
)

def chunk(text="фрагмент", source="КР ГТР", gid="g1", topic="РЕКОМЕНДАЦИИ", score=0.9):
    return RAGChunk(text=text, source_title=source, guideline_id=gid, topic=topic, score=score)

def test_redline_diagnosis_detected():
    assert detect_redline("Подтверди диагноз — это депрессия?") == REDLINE_DIAGNOSIS

def test_redline_treatment_detected():
    assert detect_redline("Каким лекарством лечить эту тревогу?") == REDLINE_TREATMENT

def test_redline_none_for_reference_query():
    assert detect_redline("Что указано в инструкции АВИАНДР?") is None

def test_refusal_on_diagnosis_query():
    a = build_rag_answer("поставь диагноз", [chunk()])
    assert a.refused and a.refusal_code == REDLINE_DIAGNOSIS
    assert not a.grounded

def test_refusal_on_treatment_query():
    a = build_rag_answer("какой препарат назначить", [chunk()])
    assert a.refused and a.refusal_code == REDLINE_TREATMENT

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
