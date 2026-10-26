"""FR-43 (docs/18 §5.3–5.4): черновик протокола с обоснованием из базы знаний.

Контракт: модель МОЖЕТ предложить диагноз и лечение, но каждый элемент обязан
ссылаться на чанк KB, реально полученный в этом запросе. Проверяется:

- источник не из выдачи → элемент отброшен (`source_missing`);
- препарата нет в указанном чанке → `drug_not_in_source` (позиция остаётся);
- дозы нет в том же чанке / доза другого числа → `dosage_not_in_source`;
- МКБ-10 не подтверждён источником → `icd10_unverified`;
- KB недоступна → черновик без `sources` + `sources_unavailable`, без 502;
- `sources` попадают в `sections` и в `content_sha256`;
- approve без обязательных полей по-прежнему не проходит.

Сеть не нужна: `search_kb` и LLM подменяются.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.errors import Problem
from app.services import protocols, session_ai
from compass.agent import RAGChunk

from .test_session_ai import BASE, _ctx, _enable, _fake_llm, _fio, _transcribed

PRESCRIPTION = "Сертралин назначают внутрь, стартовая доза 25 мг, средняя 50 мг/сут."
GUIDELINE = "При генерализованном тревожном расстройстве (F41.1) рекомендована КПТ."

CHUNKS = [
    RAGChunk(
        text=PRESCRIPTION,
        source_title="ГРЛС: сертралин",
        guideline_id="grls-sertraline",
        topic="ЛЕКАРСТВА",
        score=0.91,
        chunk_id="11111111-1111-1111-1111-111111111111",
        url="https://grls.example/sertraline",
    ),
    RAGChunk(
        text=GUIDELINE,
        source_title="КР: тревожные расстройства",
        guideline_id="kr-gad",
        topic="РЕКОМЕНДАЦИИ",
        score=0.83,
        chunk_id="22222222-2222-2222-2222-222222222222",
    ),
]

LLM_GROUNDED = json.dumps(
    {
        "complaints": "тревога, нарушение сна [S2]",
        "anamnesis_morbi": "впервые отметил после перенесённой инфекции",
        "mental_status": {"suicidal_risk": "отрицает", "emotions_volition": "тревожен"},
        "somatic_status": "без острой патологии",
        "recommendations": "КПТ, контроль через 2 недели [S2]",
        "diagnosis": {
            "syndrome": "генерализованное тревожное расстройство",
            "icd10": "F41.1",
            "rationale": "критерии ГТР по источнику",
            "sources": ["S2"],
        },
        "treatment": [
            {
                "drug": "сертралин",
                "dose": "50 мг",
                "schedule": "утром",
                "duration": "4 недели",
                "sources": ["S1"],
            }
        ],
    },
    ensure_ascii=False,
)


class _FakeKb:
    """Фабрика агента с подменённым поиском: чанки фиксированы, вызов записан."""

    def __init__(self, chunks=None, error: Exception | None = None) -> None:
        self._chunks = CHUNKS if chunks is None else chunks
        self._error = error
        self.calls: list[dict] = []

    def search_kb(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        if self._error is not None:
            raise self._error
        return list(self._chunks)


def _kb(monkeypatch, chunks=None, error: Exception | None = None) -> _FakeKb:
    fake = _FakeKb(chunks, error)
    monkeypatch.setattr(session_ai, "shared_factory", lambda: fake)
    return fake


def _sources(*chunks: RAGChunk) -> list[dict]:
    return [session_ai._source_entry(i, chunk) for i, chunk in enumerate(chunks, start=1)]


def _needs(needs: list[dict], **want) -> bool:
    return any(all(item.get(key) == value for key, value in want.items()) for item in needs)


def _sanitize(raw: dict, sources: list[dict] | None):
    return session_ai._sanitize(
        raw,
        visit_at="2026-10-26T10:00:00+03:00",
        channel="in_person",
        legal_basis="ИДС v1",
        sources=sources,
    )


# --- grounding: матрица проверок -------------------------------------------


def test_grounded_diagnosis_and_treatment_keep_sources():
    sections, needs = _sanitize(json.loads(LLM_GROUNDED), _sources(*CHUNKS))

    assert sections["diagnosis"]["icd10"] == "F41.1"
    assert sections["diagnosis"]["sources"] == [CHUNKS[1].chunk_id]
    assert [row["drug"] for row in sections["treatment"]] == ["сертралин"]
    assert sections["treatment"][0]["dose"] == "50 мг"
    assert sections["treatment"][0]["sources"] == [CHUNKS[0].chunk_id]

    stored = {source["id"]: source for source in sections["sources"]}
    assert stored[CHUNKS[0].chunk_id]["used_by"] == ["treatment.0"]
    assert stored[CHUNKS[1].chunk_id]["used_by"] == [
        "diagnosis",
        "complaints",
        "recommendations",
    ]
    assert stored[CHUNKS[0].chunk_id]["quote"] == PRESCRIPTION
    assert stored[CHUNKS[0].chunk_id]["url"] == "https://grls.example/sertraline"
    assert "text" not in stored[CHUNKS[0].chunk_id]  # полный чанк в документ не пишется
    assert not _needs(needs, reason="source_missing")
    assert not _needs(needs, reason="drug_not_in_source")
    assert not _needs(needs, reason="dosage_not_in_source")


def test_source_outside_retrieval_drops_element():
    raw = {
        "diagnosis": {"syndrome": "ГТР", "icd10": "F41.1", "sources": ["S9"]},
        "treatment": [{"drug": "сертралин", "dose": "50 мг", "sources": ["нет-такого-чанка"]}],
    }
    sections, needs = _sanitize(raw, _sources(*CHUNKS))

    assert "diagnosis" not in sections
    assert sections["treatment"] == []
    assert _needs(needs, section="diagnosis", reason="source_missing")
    # Отброшенной строки в документе нет, поэтому причина — на раздел целиком.
    assert _needs(needs, section="treatment", reason="source_missing")
    # Источники без обоснованных элементов остаются пустыми по used_by.
    assert all(source["used_by"] == [] for source in sections["sources"])


def test_drug_absent_in_cited_chunk_is_flagged_not_dropped():
    raw = {"treatment": [{"drug": "пароксетин", "dose": "20 мг", "sources": ["S1"]}]}
    sections, needs = _sanitize(raw, _sources(*CHUNKS))

    assert [row["drug"] for row in sections["treatment"]] == ["пароксетин"]
    assert _needs(needs, section="treatment.0", reason="drug_not_in_source")


def test_dosage_must_be_in_same_chunk_as_drug():
    raw = {
        "treatment": [
            # Препарат в S1, доза другого числа: «200 мг» в чанке нет.
            {"drug": "сертралин", "dose": "200 мг", "sources": ["S1"]},
            # Ложное подтверждение по подстроке: «50 мг» входит в «150 мг».
            {"drug": "сертралин", "dose": "50 мг", "sources": ["S2"]},
        ]
    }
    sections, needs = _sanitize(raw, _sources(*CHUNKS))

    assert len(sections["treatment"]) == 2  # элемент остаётся: решает врач
    assert _needs(needs, section="treatment.0", reason="dosage_not_in_source")
    # «сертралин» в S2 нет вовсе — сначала срабатывает препарат.
    assert _needs(needs, section="treatment.1", reason="drug_not_in_source")

    # Граница числа: «150 мг» не подтверждает дозу «50 мг».
    tight = RAGChunk(
        text="Доза 150 мг внутрь однократно.",
        source_title="ГРЛС: другой препарат",
        guideline_id="grls-other",
        topic="ЛЕКАРСТВА",
        chunk_id="33333333-3333-3333-3333-333333333333",
    )
    raw = {"treatment": [{"drug": "сертралин", "dose": "50 мг", "sources": ["S1"]}]}
    chunk = RAGChunk(text=f"сертралин. {tight.text}", source_title=tight.source_title,
                     guideline_id=tight.guideline_id, topic=tight.topic,
                     chunk_id=tight.chunk_id)
    _, needs = _sanitize(raw, _sources(chunk))
    assert _needs(needs, section="treatment.0", reason="dosage_not_in_source")


def test_dosage_decimal_separator_is_normalized():
    chunk = RAGChunk(
        text="Алпразолам 0.5 мг внутрь при тревоге.",
        source_title="ГРЛС: алпразолам",
        guideline_id="grls-alprazolam",
        topic="ЛЕКАРСТВА",
        chunk_id="44444444-4444-4444-4444-444444444444",
    )
    raw = {"treatment": [{"drug": "алпразолам", "dose": "0,5 мг", "sources": ["S1"]}]}
    _, needs = _sanitize(raw, _sources(chunk))
    assert not _needs(needs, section="treatment.0", reason="dosage_not_in_source")


def test_icd10_unverified_is_flagged_and_broken_code_dropped():
    raw = {
        "diagnosis": {"syndrome": "ГТР", "icd10": "F99.9", "sources": ["S2"]},
        "treatment": [],
    }
    sections, needs = _sanitize(raw, _sources(*CHUNKS))
    # Код валиден по формату, но в источнике его нет: остаётся врачу с бейджем.
    assert sections["diagnosis"]["icd10"] == "F99.9"
    assert _needs(needs, section="diagnosis", reason="icd10_unverified")

    broken = {"diagnosis": {"syndrome": "ГТР", "icd10": "41.1", "sources": ["S2"]}}
    sections, needs = _sanitize(broken, _sources(*CHUNKS))
    assert sections["diagnosis"]["icd10"] == ""  # невалидный формат не тащим в документ
    assert _needs(needs, section="diagnosis", reason="icd10_unverified")


def test_kb_unavailable_keeps_draft_with_flag():
    _, needs = _sanitize(json.loads(LLM_GROUNDED), None)

    sections_off, _ = _sanitize({"complaints": "тревога"}, None)
    assert sections_off["sources"] == []
    assert _needs(needs, section="treatment", reason="sources_unavailable")
    # Без выдачи ссылаться не на что: диагноз/лечение отброшены, а не выдуманы.
    assert _needs(needs, section="diagnosis", reason="source_missing")
    assert _needs(needs, section="mental_status.suicidal_risk", reason="clinical_risk")


def test_draft_never_approves_protocol():
    """Инвариант §5.3: сервис черновика не умеет менять статус на approved."""
    import inspect

    source = inspect.getsource(session_ai.draft)
    assert '"approved"' not in source
    assert "approve(" not in source


# --- поиск в KB для черновика ----------------------------------------------


def test_draft_retrieval_uses_topics_and_allow_redline(monkeypatch):
    fake = _kb(monkeypatch)
    monkeypatch.setattr(settings, "session_ai_sources", 7)

    chunks = session_ai._kb_sources("жалобы: тревога, бессонница; назначаем лечение и дозу")

    assert [chunk["id"] for chunk in chunks] == [CHUNKS[0].chunk_id, CHUNKS[1].chunk_id]
    assert fake.calls and fake.calls[0]["allow_redline"] is True
    assert fake.calls[0]["topics"] == session_ai.SOURCE_TOPICS
    assert fake.calls[0]["limit"] == 7
    # Промпт получает нумерованный список с метками S1/S2 и цитатами.
    block = session_ai._sources_block(chunks)
    assert "S1 · ГРЛС: сертралин · раздел ЛЕКАРСТВА" in block
    assert PRESCRIPTION in block
    # Полный текст чанка нужен grounding'у (препарат/доза «в том же чанке»),
    # а в документ попадает только короткая цитата (§5.4).
    assert chunks[0]["text"] == PRESCRIPTION


def test_draft_retrieval_disabled_counts_as_unavailable(monkeypatch):
    """CC_SESSION_AI_SOURCES=0 — «источников нет», а не «всё в порядке»."""
    fake = _kb(monkeypatch)
    monkeypatch.setattr(settings, "session_ai_sources", 0)

    assert session_ai._kb_sources("тревога") is None
    assert fake.calls == []
    _, needs = _sanitize({"complaints": "тревога"}, None)
    assert _needs(needs, section="treatment", reason="sources_unavailable")


def test_drug_lookalike_root_is_not_a_source_proof():
    """Почти-омоним не считается подтверждением: «пароксетин» ≠ «пароксизм»."""
    chunk = RAGChunk(
        text="Пароксизм тревоги купируют дыхательными упражнениями.",
        source_title="КР: панические атаки",
        guideline_id="kr-panic",
        topic="ЛЕКАРСТВА",
        chunk_id="55555555-5555-5555-5555-555555555555",
    )
    raw = {"treatment": [{"drug": "пароксетин", "dose": "20 мг", "sources": ["S1"]}]}
    _, needs = _sanitize(raw, _sources(chunk))
    assert _needs(needs, section="treatment.0", reason="drug_not_in_source")

    # Флексия того же слова — подтверждение.
    chunk = RAGChunk(
        text="Пароксетина суточная доза 20 мг.",
        source_title="ГРЛС: пароксетин",
        guideline_id="grls-paroxetine",
        topic="ЛЕКАРСТВА",
        chunk_id="66666666-6666-6666-6666-666666666666",
    )
    _, needs = _sanitize(raw, _sources(chunk))
    assert not _needs(needs, section="treatment.0", reason="drug_not_in_source")


def test_dangling_reference_in_narrative_is_flagged():
    sections, needs = _sanitize(
        {"recommendations": "КПТ [S7]", "complaints": "тревога [S1]"},
        _sources(*CHUNKS),
    )
    assert sections["recommendations"] == "КПТ [S7]"  # текст не подменяем
    assert _needs(needs, section="recommendations", reason="source_missing")
    assert not _needs(needs, section="complaints", reason="source_missing")


def test_drug_without_words_is_never_confirmed():
    """«АСК»/«Mg»/«Li» не дают слов ≥ 4 букв — пустой `all([])` подтверждал бы всё."""
    sources = _sources(*CHUNKS)
    for drug in ("АСК", "Mg", "Li"):
        sections, needs = _sanitize(
            {"treatment": [{"drug": drug, "dose": "50 мг", "sources": ["S1"]}]}, sources
        )
        assert sections["treatment"][0]["drug"] == drug  # врач может знать больше
        assert _needs(needs, section="treatment.0", reason="drug_not_in_source"), drug


def test_short_drug_inflection_is_confirmed():
    """Короткие МНН склоняются: «литий» ↔ «литием» — тот же препарат."""
    chunk = RAGChunk(
        text="Литием поддерживающая терапия 300 мг/сут.",
        source_title="ГРЛС: литий",
        guideline_id="grls-lithium",
        topic="ЛЕКАРСТВА",
        chunk_id="77777777-7777-7777-7777-777777777777",
    )
    _, needs = _sanitize(
        {"treatment": [{"drug": "литий", "dose": "300 мг", "sources": ["S1"]}]},
        _sources(chunk),
    )
    assert not _needs(needs, section="treatment.0", reason="drug_not_in_source")
    assert not _needs(needs, section="treatment.0", reason="dosage_not_in_source")


def test_decimal_separator_matches_both_ways():
    """«0.5 мг» из ответа модели подтверждается чанком «0,5 мг» и наоборот."""
    for dose, text in (("0.5 мг", "Алпразолам 0,5 мг внутрь."), ("0,5 мг", "Алпразолам 0.5 мг внутрь.")):
        chunk = RAGChunk(
            text=text,
            source_title="ГРЛС: алпразолам",
            guideline_id="grls-alprazolam",
            topic="ЛЕКАРСТВА",
            chunk_id="88888888-8888-8888-8888-888888888888",
        )
        _, needs = _sanitize(
            {"treatment": [{"drug": "алпразолам", "dose": dose, "sources": ["S1"]}]},
            _sources(chunk),
        )
        assert not _needs(needs, section="treatment.0", reason="dosage_not_in_source"), dose


def test_flags_follow_saved_rows_after_compaction():
    """Отброшенный элемент не сдвигает флаги на чужую строку лечения."""
    sections, needs = _sanitize(
        {
            "treatment": [
                {"drug": "кветиапин", "dose": "25 мг", "sources": ["S9"]},  # отброшен
                {"drug": "галоперидол", "dose": "1 мг", "sources": ["S1"]},  # сохранён
            ]
        },
        _sources(*CHUNKS),
    )
    assert [row["drug"] for row in sections["treatment"]] == ["галоперидол"]
    assert _needs(needs, section="treatment", reason="source_missing")
    assert _needs(needs, section="treatment.0", reason="drug_not_in_source")
    assert not _needs(needs, section="treatment.0", reason="source_missing")
    assert not _needs(needs, section="treatment.1", reason="drug_not_in_source")


def test_used_by_has_no_duplicates():
    sections, _ = _sanitize(
        {"complaints": "тревога [S1], бессонница [S1]", "recommendations": "КПТ [S1]"},
        _sources(*CHUNKS),
    )
    used = next(item for item in sections["sources"] if item["id"] == CHUNKS[0].chunk_id)
    assert used["used_by"] == ["complaints", "recommendations"]


def test_validate_sections_rejects_source_outside_document():
    """Ссылка на чанк, которого нет в `sources` документа, — 422."""
    sections = {
        "diagnosis": {"icd10": "F41.1", "sources": ["aaaaaaaa-0000-0000-0000-000000000000"]},
        "sources": [
            {
                "id": "bbbbbbbb-0000-0000-0000-000000000000",
                "section": "ЛЕКАРСТВА",
                "title": "ГРЛС",
                "url": "",
                "quote": "…",
                "used_by": ["diagnosis"],
            }
        ],
    }
    with pytest.raises(Problem) as caught:
        protocols.validate_sections(sections)
    assert caught.value.code == "PROTOCOL_SOURCE_UNKNOWN"


def test_short_name_needs_its_own_word():
    """«АСК» подтверждается только отдельным словом, а не «аскаридозом»."""
    acarid = RAGChunk(
        text="Аскаридоз лечат альбендазолом 400 мг однократно.",
        source_title="КР: гельминтозы",
        guideline_id="kr-helminth",
        topic="ЛЕКАРСТВА",
        chunk_id="99999999-9999-9999-9999-999999999999",
    )
    raw = {"treatment": [{"drug": "АСК", "dose": "50 мг", "sources": ["S1"]}]}
    _, needs = _sanitize(raw, _sources(acarid))
    assert _needs(needs, section="treatment.0", reason="drug_not_in_source")

    aspirin = RAGChunk(
        text="АСК 50 мг — антиагрегантная доза при ИБС.",
        source_title="КР: ИБС",
        guideline_id="kr-ihd",
        topic="ЛЕКАРСТВА",
        chunk_id="aaaaaaaa-1111-1111-1111-111111111111",
    )
    _, needs = _sanitize(raw, _sources(aspirin))
    assert not _needs(needs, section="treatment.0", reason="drug_not_in_source")


def test_prefix_of_another_word_is_not_a_source_proof():
    """«Аспирин» не подтверждается «аспирацией»: корни должны совпасть."""
    aspiration = RAGChunk(
        text="Аспирация содержимого желудка при отравлении.",
        source_title="КР: неотложные состояния",
        guideline_id="kr-emergency",
        topic="ЛЕКАРСТВА",
        chunk_id="bbbbbbbb-2222-2222-2222-222222222222",
    )
    raw = {"treatment": [{"drug": "Аспирин", "dose": "75 мг", "sources": ["S1"]}]}
    _, needs = _sanitize(raw, _sources(aspiration))
    assert _needs(needs, section="treatment.0", reason="drug_not_in_source")

    aspirin = RAGChunk(
        text="Аспирина 75 мг на ночь.",
        source_title="ГРЛС: аспирин",
        guideline_id="grls-aspirin",
        topic="ЛЕКАРСТВА",
        chunk_id="cccccccc-3333-3333-3333-333333333333",
    )
    _, needs = _sanitize(raw, _sources(aspirin))
    assert not _needs(needs, section="treatment.0", reason="drug_not_in_source")


def test_citation_quote_uses_schema_cap():
    """Потолок цитаты — один на пакет (`protocols.SOURCE_QUOTE_CHARS`)."""
    assert session_ai.SOURCE_QUOTE_CHARS == protocols.SOURCE_QUOTE_CHARS

    long_chunk = RAGChunk(
        text="Сертралин. " + "Доза титруется постепенно. " * 40,
        source_title="ГРЛС: сертралин",
        guideline_id="grls-sertraline",
        topic="ЛЕКАРСТВА",
        chunk_id="dddddddd-4444-4444-4444-444444444444",
    )
    entry = session_ai._source_entry(1, long_chunk)
    assert 320 < len(entry["quote"]) <= protocols.SOURCE_QUOTE_CHARS

    huge_chunk = RAGChunk(
        text="Сертралин. " + "Доза титруется постепенно. " * 200,
        source_title="ГРЛС: сертралин",
        guideline_id="grls-sertraline",
        topic="ЛЕКАРСТВА",
        chunk_id="eeeeeeee-5555-5555-5555-555555555555",
    )
    quote = session_ai._source_entry(1, huge_chunk)["quote"]
    assert len(quote) <= protocols.SOURCE_QUOTE_CHARS + 1  # + многоточие
    assert quote.endswith("…")
    stored = {key: value for key, value in entry.items() if key != "text"}
    protocols.validate_sections({"sources": [stored]})


# --- сквозной путь через API ------------------------------------------------


def test_draft_through_api_stores_sources_and_hashes_them(client, monkeypatch, db_session):
    _enable(monkeypatch)
    ctx = _ctx(client, "ground")
    _fio(db_session, ctx)
    session, _ = _transcribed(client, ctx, monkeypatch)
    fake = _kb(monkeypatch)
    _fake_llm(monkeypatch, LLM_GROUNDED)
    sid = session["id"]

    resp = client.post(f"{BASE}/sessions/{sid}/protocol/draft", headers=ctx["doctor"])
    assert resp.status_code == 201, resp.text
    draft = resp.json()
    sections = draft["sections"]

    assert draft["prompt_version"] == "v2"
    assert fake.calls[0]["allow_redline"] is True
    assert sections["diagnosis"]["icd10"] == "F41.1"
    assert sections["diagnosis"]["sources"] == [CHUNKS[1].chunk_id]
    assert sections["treatment"][0]["drug"] == "сертралин"
    assert [source["ref"] for source in sections["sources"]] == ["S1", "S2"]
    assert "treatment.0" in sections["sources"][0]["used_by"]

    # Черновик не подписан: content_sha256 считается при approve (docs/18 §5.2).
    assert draft["content_sha256"] is None

    # Формат МКБ-10 grounding не ослабляет: невалидный код — по-прежнему 422.
    bad = client.put(
        f"{BASE}/sessions/{sid}/protocol",
        json={"sections": {**sections, "diagnosis": {"icd10": "41.1", "sources": []}}},
        headers=ctx["doctor"],
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["code"] == "PROTOCOL_ICD10_INVALID"

    # Без обязательного кода approve не проходит — черновик сам себя не утверждает.
    cleared = {**sections, "diagnosis": {**sections["diagnosis"], "icd10": ""}}
    assert client.put(
        f"{BASE}/sessions/{sid}/protocol",
        json={"sections": cleared},
        headers=ctx["doctor"],
    ).status_code == 200
    not_ready = client.post(f"{BASE}/sessions/{sid}/protocol/approve", headers=ctx["doctor"])
    assert not_ready.status_code == 409
    assert not_ready.json()["code"] == "PROTOCOL_NOT_READY"
    assert "diagnosis.icd10" in not_ready.json()["detail"]

    # Врач дописал код и утвердил: хэш считается по каноническому JSON, и sources
    # входят в него — обоснование подписывается вместе с документом (§5.4).
    final = {**sections, "diagnosis": {**sections["diagnosis"], "icd10": "F41.1"}}
    assert client.put(
        f"{BASE}/sessions/{sid}/protocol",
        json={"sections": final},
        headers=ctx["doctor"],
    ).status_code == 200
    approved = client.post(f"{BASE}/sessions/{sid}/protocol/approve", headers=ctx["doctor"])
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["content_sha256"] == protocols.content_sha256(body["sections"])
    assert body["content_sha256"] != protocols.content_sha256({**final, "sources": []})


def test_draft_through_api_without_kb_flags_sources_unavailable(client, monkeypatch, db_session):
    _enable(monkeypatch)
    ctx = _ctx(client, "nokb")
    _fio(db_session, ctx)
    session, _ = _transcribed(client, ctx, monkeypatch)
    _kb(monkeypatch, error=TimeoutError("qdrant timeout"))
    captured = _fake_llm(monkeypatch, LLM_GROUNDED)
    sid = session["id"]

    resp = client.post(f"{BASE}/sessions/{sid}/protocol/draft", headers=ctx["doctor"])
    assert resp.status_code == 201, resp.text
    draft = resp.json()

    assert draft["sections"]["sources"] == []
    assert "diagnosis" not in draft["sections"]
    assert draft["sections"]["treatment"] == []
    reasons = {(item["section"], item["reason"]) for item in draft["needs_review"]}
    assert ("treatment", "sources_unavailable") in reasons
    assert ("diagnosis", "source_missing") in reasons
    # Модель видела, что источников нет, и промпт это фиксирует — не тихая выдумка.
    user = captured["messages"][-1][1]["content"]
    assert "источники недоступны" in user
