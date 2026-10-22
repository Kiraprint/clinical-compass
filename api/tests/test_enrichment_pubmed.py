"""Коннектор PubMed: XML-разбор, план запроса, обработка ошибок (оффлайн)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app.enrichment import pubmed
from app.enrichment.config import PubMedConfig

from .enrichment_fixtures import PUBMED_XML


def _cfg(**overrides) -> PubMedConfig:
    base = dict(email="team@example.org", api_key="k-123", retmax=1000, days=7)
    base.update(overrides)
    return PubMedConfig(**base)


def test_parse_structured_abstract_and_metadata():
    docs = pubmed.parse_pubmed_xml(PUBMED_XML)
    assert [d.external_id for d in docs] == ["40000001", "40000002", "40000003"]

    first = docs[0]
    assert first.guideline_id == "pmid-40000001"
    assert first.doi == "10.1016/j.jad.2024.000001"
    assert first.pmcid == "PMC10000001"
    assert first.year == 2024
    assert first.language == "eng"
    assert first.authors == ("Ivanova AA", "Petrov BB")
    assert "Meta-Analysis" in first.publication_types
    assert first.results.startswith("CBT reduced anxiety symptoms")
    assert "BACKGROUND:" in first.abstract
    assert first.retraction == "correction"
    assert "Retraction" not in first.retraction_note
    assert first.fulltext_url == "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10000001/"
    assert first.revision.endswith(first.revision.split("#")[1])


def test_parse_detects_retraction():
    docs = pubmed.parse_pubmed_xml(PUBMED_XML)
    retracted = docs[1]
    assert retracted.retraction == "retracted"
    assert "Retraction" in retracted.retraction_note
    assert retracted.doi == ""


def test_parse_medline_date_and_unstructured_abstract():
    docs = pubmed.parse_pubmed_xml(PUBMED_XML)
    third = docs[2]
    assert third.year == 2023
    assert third.results == ""
    assert third.retraction == "none"


def test_parse_malformed_xml_raises_source_error():
    with pytest.raises(pubmed.SourceError):
        pubmed.parse_pubmed_xml("<not-xml")


def test_search_params_include_window_language_and_key():
    params = pubmed.search_params(_cfg(), today=dt.date(2026, 10, 21))
    assert params["db"] == "pubmed"
    assert "Systematic Review" in params["term"]
    assert params["datetype"] == "pdat"
    assert params["mindate"] == "2026/10/14"
    assert params["maxdate"] == "2026/10/21"
    assert params["retmax"] == "500"  # clamp
    assert params["email"] == "team@example.org"
    assert params["api_key"] == "k-123"
    assert "api_key" not in str(pubmed.search_params(_cfg(api_key=""), today=dt.date(2026, 10, 21)))


def test_fetch_documents_uses_both_endpoints(monkeypatch):
    calls: list[str] = []

    def fake_request(cfg, url):
        calls.append(url)
        if "esearch" in url:
            return json.dumps(
                {"esearchresult": {"count": "3", "idlist": ["40000001", "40000002", "40000003"]}}
            ).encode()
        return PUBMED_XML.encode()

    monkeypatch.setattr(pubmed, "_request", fake_request)
    docs, plan, errors = pubmed.fetch_documents(_cfg(), today=dt.date(2026, 10, 21))
    assert len(docs) == 3
    assert errors == []
    assert plan["count"] == 3 and plan["returned"] == 3
    assert "api_key" not in plan["params"]
    assert any("esearch" in url for url in calls) and any("efetch" in url for url in calls)


def test_fetch_documents_reports_bad_esearch(monkeypatch):
    def fake_request(cfg, url):
        return b'{"esearchresult": {"ERROR": "Invalid query"}}'

    monkeypatch.setattr(pubmed, "_request", fake_request)
    with pytest.raises(pubmed.SourceError):
        pubmed.fetch_documents(_cfg())


def test_parse_without_abstract_or_doi_is_honest():
    xml = """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID>
    <Article><Journal><Title>J</Title><JournalIssue><PubDate><Year>2026</Year></PubDate></JournalIssue></Journal>
    <ArticleTitle>Depression review</ArticleTitle><Language>eng</Language>
    <PublicationTypeList><PublicationType>Systematic Review</PublicationType></PublicationTypeList>
    </Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    doc = pubmed.parse_pubmed_xml(xml)[0]
    assert doc.abstract == ""
    assert doc.doi == ""
    assert doc.url == "https://pubmed.ncbi.nlm.nih.gov/1/"
    assert doc.fulltext_url == doc.url
