"""Домен обогащения: дедуп, дизайн, критерии, AMSTAR 2, чанкинг (без сети)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.enrichment import appraisal, criteria, design, index
from app.enrichment import dedupe
from app.enrichment.types import (
    VERDICT_INSUFFICIENT,
    VERDICT_MET,
    VERDICT_NOT_CHECKED,
    VERDICT_NOT_MET,
)
from app.models import KBWork

from .enrichment_fixtures import make_doc, make_guideline

TODAY = dt.date(2026, 10, 21)


# --- Дедупликация ---------------------------------------------------------


def test_normalize_doi_strips_prefixes_and_case():
    assert dedupe.normalize_doi("https://doi.org/10.1016/J.JAD.2024.1") == "10.1016/j.jad.2024.1"
    assert dedupe.normalize_doi("doi: 10.1000/XYZ ") == "10.1000/xyz"
    assert dedupe.normalize_doi("") == ""


def test_title_key_normalizes_punctuation_and_yo():
    left = dedupe.title_key("Тревога и депрессия: обзор — 2024", 2024)
    right = dedupe.title_key("тревога и депрессия обзор 2024", 2024)
    assert left == right
    assert dedupe.title_key("Title", None) != dedupe.title_key("Title", 2024)


def test_duplicate_key_detects_same_doi_within_batch():
    first = make_doc(doi="10.1/ABC", pmid="1", external_id="1")
    second = make_doc(doi="https://doi.org/10.1/abc", pmid="2", external_id="2", title="Other")
    seen: dict[str, str] = {}
    assert dedupe.duplicate_key(first, seen) is None
    dedupe.remember(first, seen)
    assert dedupe.duplicate_key(second, seen) == "doi:10.1/abc"


def test_duplicate_key_detects_title_year_match():
    first = make_doc(pmid="1", external_id="1", doi="")
    second = make_doc(
        source="who", external_id="who-1", guideline_id="who-1", pmid="", doi="", kind="article"
    )
    seen: dict[str, str] = {}
    dedupe.remember(first, seen)
    assert dedupe.duplicate_key(second, seen) == f"title:{dedupe.title_key(first.title, first.year)}"


# --- Классификация дизайна ------------------------------------------------


@pytest.mark.parametrize(
    ("title", "types", "expected"),
    [
        (
            "CBT for GAD: a systematic review and meta-analysis",
            ("Meta-Analysis", "Systematic Review"),
            "systematic_review_meta_analysis",
        ),
        ("Drug X versus placebo: a meta-analysis", ("Meta-Analysis",), "meta_analysis"),
        (
            "Comparative efficacy: a network meta-analysis",
            ("Meta-Analysis",),
            "network_meta_analysis",
        ),
        (
            "An umbrella review of systematic reviews in depression",
            ("Review",),
            "umbrella_review",
        ),
        ("A randomised controlled trial of exercise", ("Randomized Controlled Trial",), "rct"),
        ("WHO guideline on mental health at work", (), "other"),
    ],
)
def test_design_rules(title, types, expected):
    doc = make_doc(title=title, publication_types=types)
    result = design.classify(doc)
    assert result.design == expected
    assert result.confidence in {"high", "medium", "low"}


def test_design_guideline_kind():
    assert design.classify(make_guideline()).design == "guideline"


def test_design_unknown_is_low_confidence():
    doc = make_doc(title="Something about depression", publication_types=("Journal Article",))
    result = design.classify(doc)
    assert result.design == "other"
    assert result.confidence == "low"


# --- Критерии -------------------------------------------------------------


def test_article_meets_all_hard_criteria():
    doc = make_doc()
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    assert evaluation.decision == "include"
    statuses = {v.key: v.status for v in evaluation.verdicts}
    assert statuses["psychiatry_relevance"] == VERDICT_MET
    assert statuses["freshness"] == VERDICT_MET
    assert statuses["design"] == VERDICT_MET
    assert statuses["language"] == VERDICT_MET
    assert statuses["retraction"] == VERDICT_MET
    assert evaluation.reasons == ()


def test_freshness_boundary_inclusive():
    boundary = make_doc(year=TODAY.year - 5)
    older = make_doc(year=TODAY.year - 6)
    assert criteria.freshness(boundary, TODAY).status == VERDICT_MET
    assert criteria.freshness(older, TODAY).status == VERDICT_NOT_MET


def test_unknown_year_is_insufficient_not_exclusion():
    doc = make_doc(year=None)
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    verdict = evaluation.by_key("freshness")
    assert verdict is not None and verdict.status == VERDICT_INSUFFICIENT
    assert evaluation.decision == "include"  # неизвестное не считается недостатком
    assert verdict.manual_review is True


def test_non_psychiatric_article_is_excluded_with_reason():
    doc = make_doc(
        title="Statin therapy for hyperlipidaemia: a meta-analysis",
        abstract="METHODS: We pooled 12 trials of statins in adults with high cholesterol. RESULTS: LDL fell by 40%.",
        results="LDL fell by 40%.",
        text="",
    )
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    assert evaluation.decision == "exclude"
    assert any("релевантность" in reason.lower() for reason in evaluation.reasons)


def test_retracted_article_is_excluded():
    doc = make_doc(retraction="retracted", retraction_note="Retraction: data fabrication")
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    assert evaluation.decision == "exclude"
    verdict = evaluation.by_key("retraction")
    assert verdict is not None and verdict.status == VERDICT_NOT_MET
    assert verdict.manual_review is True


def test_unknown_retraction_is_not_checked_not_exclusion():
    doc = make_doc(retraction="unknown")
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    assert evaluation.decision == "include"
    assert evaluation.by_key("retraction").status == VERDICT_NOT_CHECKED


def test_foreign_language_is_excluded():
    doc = make_doc(language="ger")
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    assert evaluation.decision == "exclude"
    assert evaluation.by_key("language").status == VERDICT_NOT_MET


def test_missing_results_section_is_insufficient_not_exclusion():
    doc = make_doc(results="")
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    assert evaluation.decision == "include"
    assert evaluation.by_key("reporting_completeness").status == VERDICT_INSUFFICIENT


def test_guideline_with_confirmed_license_is_included_and_license_met():
    doc = make_guideline()
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    assert evaluation.decision == "include"
    assert evaluation.by_key("license").status == VERDICT_MET
    assert evaluation.by_key("recency").status == VERDICT_MET


def test_guideline_without_license_is_insufficient_but_not_excluded():
    doc = make_guideline(license="", text=make_guideline().text.replace("Creative Commons", "Rights"))
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    assert evaluation.decision == "include"
    assert evaluation.by_key("license").status == VERDICT_INSUFFICIENT
    assert evaluation.by_key("license").manual_review is True


def test_old_guideline_recency_is_warning_not_exclusion():
    doc = make_guideline(year=2013)
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    assert evaluation.decision == "include"
    assert evaluation.by_key("recency").status == VERDICT_NOT_MET
    assert evaluation.by_key("recency").manual_review is True


def test_short_guideline_text_is_excluded():
    doc = make_guideline(text="mental health guideline")
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    assert evaluation.decision == "exclude"
    assert evaluation.by_key("text_available").status == VERDICT_NOT_MET


# --- AMSTAR 2 -------------------------------------------------------------


def test_amstar2_uses_abstract_and_marks_fulltext_items_insufficient():
    doc = make_doc()
    result = appraisal.appraise(doc, design.classify(doc))
    assert result.tool == "amstar2"
    assert result.applicable is True
    assert result.experimental is True
    assert len(result.items) == 16
    statuses = {item.key: item.status for item in result.items}
    assert statuses["protocol"] == VERDICT_MET          # PROSPERO? — нет: см. ниже
    assert statuses["search"] == VERDICT_MET
    assert statuses["meta_methods"] == VERDICT_MET
    assert statuses["publication_bias"] == VERDICT_MET
    assert statuses["excluded_list"] == VERDICT_INSUFFICIENT
    assert statuses["rob_impact"] == VERDICT_INSUFFICIENT


def test_amstar2_protocol_not_reported_is_insufficient():
    doc = make_doc(abstract="BACKGROUND: Generalised anxiety. METHODS: We searched PubMed and Embase.")
    result = appraisal.appraise(doc, design.classify(doc))
    statuses = {item.key: item.status for item in result.items}
    assert statuses["protocol"] == VERDICT_INSUFFICIENT


def test_all_evidence_quotes_are_exact_substrings():
    doc = make_doc()
    result = appraisal.appraise(doc, design.classify(doc))
    for item in result.items:
        if item.evidence is None:
            continue
        assert item.evidence.quote
        assert item.evidence.quote in doc.abstract
        assert item.evidence.locator == "abstract"


def test_appraisal_tool_follows_design():
    rct = make_doc(publication_types=("Randomized Controlled Trial",), title="Trial of CBT")
    assert appraisal.appraise(rct, design.classify(rct)).tool == "rob2"
    cohort = make_doc(publication_types=("Observational Study",), title="Cohort of patients")
    assert appraisal.appraise(cohort, design.classify(cohort)).tool == "robins_i"
    guideline = make_guideline()
    assert appraisal.appraise(guideline, design.classify(guideline)).tool == "agree2"
    narrative = make_doc(publication_types=("Review",), title="Narrative overview")
    assert appraisal.appraise(narrative, design.classify(narrative)).tool == "none"


# --- Чанкинг и документы индекса -----------------------------------------


def test_chunk_text_preserves_content_and_limits_size():
    text = "\n\n".join(f"Параграф {i}. " + "слово " * 120 for i in range(12))
    chunks = index.chunk_text(text, size=600, overlap=80)
    assert len(chunks) > 1
    assert all(len(chunk) <= 700 for chunk in chunks)
    assert chunks[0].startswith("Параграф 0")
    assert chunks[-1].rstrip().endswith("слово")


def test_chunk_text_is_deterministic_and_deduplicates():
    text = "А" * 3000
    first = index.chunk_text(text, size=800, overlap=100)
    second = index.chunk_text(text, size=800, overlap=100)
    assert first == second
    assert len(first) == len(set(first))


def _work(kind: str, **overrides) -> KBWork:
    work = KBWork(
        kind=kind,
        source="pubmed" if kind == "article" else "who",
        external_id="40000001" if kind == "article" else "10665/374250",
        guideline_id="pmid-40000001" if kind == "article" else "who-10665-374250",
        revision="2024#abc",
        content_hash="deadbeef",
        status="approved",
        title="Cognitive behavioural therapy for generalised anxiety disorder",
        full_text="",
        meta={
            "abstract": "We pooled 27 trials.",
            "results": "SMD -0.42.",
            "url": "https://pubmed.ncbi.nlm.nih.gov/40000001/",
            "journal": "J Affect Disord",
            "year": 2024,
            "doi": "10.1/x",
            "pmid": "40000001",
            "license": "publisher terms",
        },
        design={"design": "systematic_review_meta_analysis"},
        appraisal={"tool": "amstar2", "summary": "подтверждено 7/16"},
        criteria={},
        retraction="none",
        revision_history=[],
    )
    for key, value in overrides.items():
        setattr(work, key, value)
    return work


def test_article_document_contains_provenance_link_and_topic():
    docs = index.build_documents(_work("article"))
    assert len(docs) == 1
    doc = docs[0]
    assert doc["topic"] == "НАУЧНЫЕ-СТАТЬИ"
    assert doc["guideline_id"] == "pmid-40000001"
    assert "https://pubmed.ncbi.nlm.nih.gov/40000001/" in doc["text"]
    assert "Абстракт:" in doc["text"]
    assert doc["work_id"]
    assert doc["source_url"].endswith("/40000001/")


def test_guideline_document_has_recommendation_topic_and_license_header():
    work = _work(
        "guideline",
        full_text="Рекомендация 1. " * 400,
        meta={"url": "https://iris.who.int/handle/10665/374250", "license": "CC BY-NC-SA 3.0 IGO", "year": 2023},
    )
    docs = index.build_documents(work)
    assert docs
    assert all(doc["topic"] == "РЕКОМЕНДАЦИИ" for doc in docs)
    assert "CC BY-NC-SA 3.0 IGO" in docs[0]["text"]
    assert "https://iris.who.int/handle/10665/374250" in docs[0]["text"]


def test_proprietary_who_license_is_hard_exclusion():
    doc = make_guideline(license="proprietary")
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    verdict = evaluation.by_key("license")
    assert verdict is not None and verdict.status == VERDICT_NOT_MET
    assert evaluation.decision == "exclude"


def test_unknown_cc_license_requires_manual_review_but_does_not_exclude():
    doc = make_guideline(license="other-cc")
    evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
    verdict = evaluation.by_key("license")
    assert verdict is not None and verdict.status == VERDICT_INSUFFICIENT
    assert verdict.manual_review is True
    assert evaluation.decision == "include"


def test_relevance_evidence_is_exact_substring_of_title_or_abstract():
    for doc in (make_doc(), make_guideline()):
        evaluation = criteria.evaluate(doc, design.classify(doc), TODAY)
        verdict = evaluation.by_key("psychiatry_relevance")
        assert verdict is not None and verdict.evidence is not None
        source = doc.title if verdict.evidence.locator == "title" else doc.abstract
        if verdict.evidence.locator == "fulltext":
            source = doc.text
        assert verdict.evidence.quote in source
