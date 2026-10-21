"""Фикстуры обогащения: проверенные вручную метаданные и фрагменты текста.

Никакой сети: XML/JSON ниже собраны по фактическим схемам PubMed efetch и
WHO IRIS DSpace 7, но тексты — собственные (без сторонних лицензионных
материалов)."""

from __future__ import annotations

import datetime as dt

from app.enrichment.types import SourceDocument

# --- PubMed ---------------------------------------------------------------

PUBMED_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation Status="MEDLINE" Owner="NLM">
      <PMID Version="1">40000001</PMID>
      <DateRevised><Year>2025</Year><Month>02</Month><Day>03</Day></DateRevised>
      <Article PubModel="Print">
        <Journal>
          <Title>Journal of Affective Disorders</Title>
          <JournalIssue CitedMedium="Print">
            <PubDate><Year>2024</Year><Month>Nov</Month><Day>15</Day></PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>Cognitive behavioural therapy for generalised anxiety disorder: a systematic review and meta-analysis</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Generalised anxiety disorder is common in primary care.</AbstractText>
          <AbstractText Label="METHODS">We searched PubMed, Embase and the Cochrane Library from 2010 to 2024. Two reviewers selected 27 randomized trials. Random-effects meta-analysis was used; heterogeneity was assessed with I2 and subgroup analysis.</AbstractText>
          <AbstractText Label="RESULTS">CBT reduced anxiety symptoms (standardised mean difference -0.42, 95% CI -0.55 to -0.29; I2 = 48%). Publication bias was assessed with funnel plots and Egger test.</AbstractText>
          <AbstractText Label="CONCLUSIONS">CBT is effective for generalised anxiety disorder in adults.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Ivanova</LastName><Initials>AA</Initials></Author>
          <Author><LastName>Petrov</LastName><Initials>BB</Initials></Author>
        </AuthorList>
        <Language>eng</Language>
        <PublicationTypeList>
          <PublicationType UI="D016428">Journal Article</PublicationType>
          <PublicationType UI="D017418">Meta-Analysis</PublicationType>
          <PublicationType UI="D000078182">Systematic Review</PublicationType>
        </PublicationTypeList>
        <ELocationID EIdType="doi">10.1016/J.JAD.2024.000001</ELocationID>
      </Article>
      <CommentsCorrectionsList>
        <CommentsCorrections RefType="ErratumIn">
          <RefSource>J Affect Disord. 2025 Jan;369:1.</RefSource>
        </CommentsCorrections>
      </CommentsCorrectionsList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">40000001</ArticleId>
        <ArticleId IdType="doi">10.1016/j.jad.2024.000001</ArticleId>
        <ArticleId IdType="pmc">PMC10000001</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation Status="MEDLINE" Owner="NLM">
      <PMID Version="1">40000002</PMID>
      <Article PubModel="Print">
        <Journal>
          <Title>Depression Research</Title>
          <JournalIssue CitedMedium="Print">
            <PubDate><Year>2019</Year><Month>Mar</Month></PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>Antidepressant augmentation for treatment-resistant depression: a meta-analysis</ArticleTitle>
        <Abstract>
          <AbstractText>Pooled analysis of 9 trials of augmentation strategies in treatment-resistant depression.</AbstractText>
        </Abstract>
        <Language>eng</Language>
        <PublicationTypeList>
          <PublicationType UI="D016428">Journal Article</PublicationType>
          <PublicationType UI="D016454">Retracted Publication</PublicationType>
          <PublicationType UI="D017418">Meta-Analysis</PublicationType>
        </PublicationTypeList>
      </Article>
      <CommentsCorrectionsList>
        <CommentsCorrections RefType="RetractionIn">
          <RefSource>Depress Res. 2024;12:99. Retraction: antidepressant augmentation.</RefSource>
        </CommentsCorrections>
      </CommentsCorrectionsList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">40000002</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation Status="MEDLINE" Owner="NLM">
      <PMID Version="1">40000003</PMID>
      <Article PubModel="Electronic">
        <Journal>
          <Title>Anxiety Reports</Title>
          <JournalIssue CitedMedium="Internet">
            <PubDate><MedlineDate>2023 Spring</MedlineDate></PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>Anxiety and depression comorbidity in primary care: a systematic review</ArticleTitle>
        <Abstract>
          <AbstractText>We reviewed studies on comorbid anxiety and depression without a structured results section.</AbstractText>
        </Abstract>
        <Language>eng</Language>
        <PublicationTypeList>
          <PublicationType UI="D016428">Journal Article</PublicationType>
          <PublicationType UI="D000078182">Systematic Review</PublicationType>
        </PublicationTypeList>
      </Article>
      <MedlineCitation>
      </MedlineCitation>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">40000003</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

WHO_ITEM = {
    "uuid": "d7727881-07ec-485f-95a8-6f2864aee9a9",
    "handle": "10665/374250",
    "metadata": {
        "dc.title": [{"value": "Mental Health Gap Action Programme (mhGAP) guideline for mental, neurological and substance use disorders"}],
        "dc.date.issued": [{"value": "2023-11-20"}],
        "dc.description.abstract": [{"value": "Updated WHO recommendations for priority mental, neurological and substance use disorders."}],
        "dc.language.iso": [{"value": "en"}],
        "dc.rights": [{"value": "CC BY-NC-SA 3.0 IGO"}],
    },
}

WHO_TEXT = (
    "Mental Health Gap Action Programme (mhGAP) guideline for mental, neurological and substance use disorders\n"
    "© World Health Organization 2023\n"
    "Some rights reserved. This work is available under the Creative Commons Attribution-NonCommercial-ShareAlike 3.0 IGO licence "
    "(CC BY-NC-SA 3.0 IGO; https://creativecommons.org/licenses/by-nc-sa/3.0/igo).\n"
    "\n"
    "Depression and anxiety are priority conditions. Recommendations cover psychological interventions, "
    "antidepressant treatment and follow-up in non-specialized settings.\n"
    + "Recommendation text for depression and anxiety. " * 400
)


def make_doc(**overrides) -> SourceDocument:
    base = dict(
        source="pubmed",
        kind="article",
        external_id="40000001",
        guideline_id="pmid-40000001",
        title="Cognitive behavioural therapy for generalised anxiety disorder: a systematic review and meta-analysis",
        revision="2025#abcdef123456",
        text="",
        abstract=(
            "BACKGROUND: Generalised anxiety disorder is common. "
            "METHODS: The protocol was registered in PROSPERO (CRD42024123456). "
            "We searched PubMed, Embase and the Cochrane Library. Two reviewers selected 27 randomized trials. "
            "Random-effects meta-analysis was used; heterogeneity was assessed with I2. "
            "RESULTS: CBT reduced anxiety symptoms (SMD -0.42, I2 = 48%). Publication bias was assessed with funnel plots. "
            "CONCLUSIONS: CBT is effective."
        ),
        results="CBT reduced anxiety symptoms (SMD -0.42, I2 = 48%). Publication bias was assessed with funnel plots.",
        journal="Journal of Affective Disorders",
        year=2024,
        authors=("Ivanova AA", "Petrov BB"),
        doi="10.1016/j.jad.2024.000001",
        pmid="40000001",
        pmcid="PMC10000001",
        url="https://pubmed.ncbi.nlm.nih.gov/40000001/",
        fulltext_url="https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10000001/",
        license="publisher terms (abstract only)",
        language="eng",
        publication_types=("Journal Article", "Meta-Analysis", "Systematic Review"),
        retraction="none",
        retraction_note="",
        fetched_at=dt.datetime(2026, 10, 21, tzinfo=dt.timezone.utc).isoformat(),
    )
    base.update(overrides)
    if "guideline_id" not in overrides:
        identifier = base["pmid"] or base["external_id"]
        base["guideline_id"] = f"pmid-{identifier}"
    if overrides.get("text") is None:
        base["text"] = str(base["title"]) + "\n" + str(base["abstract"]) + "\n" + str(base["results"])
    return SourceDocument(**base)


def make_guideline(**overrides) -> SourceDocument:
    base = dict(
        source="who",
        kind="guideline",
        external_id="10665/374250",
        guideline_id="who-10665-374250",
        title="Mental Health Gap Action Programme (mhGAP) guideline for mental, neurological and substance use disorders",
        revision="2023-11-20#abcdef123456",
        text=WHO_TEXT,
        abstract="Updated WHO recommendations for priority mental, neurological and substance use disorders.",
        journal="World Health Organization",
        year=2023,
        url="https://iris.who.int/handle/10665/374250",
        fulltext_url="https://iris.who.int/handle/10665/374250",
        license="CC BY-NC-SA 3.0 IGO",
        language="en",
        publication_types=("Guideline", "WHO guideline"),
        retraction="not_checked",
        fetched_at=dt.datetime(2026, 10, 21, tzinfo=dt.timezone.utc).isoformat(),
    )
    base.update(overrides)
    return SourceDocument(**base)
