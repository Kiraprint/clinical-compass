"""Коннектор PubMed (NCBI E-utilities): свежие систематические обзоры/метаанализы.

Только метаданные и абстракт: полные тексты статей не храним (лицензии
издателей), врач открывает первоисточник по ссылке (URL/DOI/PMC). Абстракт —
единственный материал для детерминированной оценки методологии; всё, что в
абстракте не reported, помечается `insufficient_info`, а не «не выполнено».

Ретраги/исправления читаются из тех же метаданных PubMed
(PublicationType + CommentsCorrectionsList) — отдельного API не нужно.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from app.enrichment.types import SourceDocument

MAX_BATCH = 100


class SourceError(RuntimeError):
    """PubMed недоступен или отдал неожиданный формат."""


def _request(cfg, url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": cfg.user_agent})
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:  # noqa: S310 — фиксированный https-хост
            return resp.read()
    except urllib.error.HTTPError as exc:  # pragma: no cover - сеть
        raise SourceError(f"PubMed HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:  # pragma: no cover - сеть
        raise SourceError(f"PubMed недоступен: {exc}") from exc


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _pub_date(article: ET.Element) -> tuple[int | None, str]:
    """Год публикации: PubDate (Year|MedlineDate) → ArticleDate → пусто."""
    for path in ("Journal/JournalIssue/PubDate", "ArticleDate"):
        node = article.find(path)
        if node is None:
            continue
        year = node.findtext("Year")
        if year and year.strip().isdigit():
            month = (node.findtext("Month") or "01").strip()
            day = (node.findtext("Day") or "01").strip()
            return int(year), f"{year}-{_month(month)}-{_day(day)}"
        medline = node.findtext("MedlineDate")
        if medline:
            match = re.search(r"(19|20)\d{2}", medline)
            if match:
                return int(match.group(0)), match.group(0)
    return None, ""


def _month(value: str) -> str:
    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
        "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    value = value.strip().lower()
    if value.isdigit():
        return value.zfill(2)
    return months.get(value[:3], "01")


def _day(value: str) -> str:
    value = value.strip()
    return value.zfill(2) if value.isdigit() else "01"


def publication_types(article: ET.Element) -> tuple[str, ...]:
    node = article.find("PublicationTypeList")
    if node is None:
        return ()
    return tuple(t.strip() for t in (node.itertext()) if t and t.strip())


def retraction_status(
    types: tuple[str, ...], corrections: list[tuple[str, str]]
) -> tuple[str, str]:
    """(status, note) по метаданным PubMed.

    retracted — статья отозвана (PublicationType Retracted Publication или
    RefType RetractionIn); correction — опубликовано исправление/уточнение.
    """
    normalized = {t.lower() for t in types}
    ref_types = {ref.lower() for ref, _ in corrections}
    notes: list[str] = []
    if "retracted publication" in normalized or "retractionin" in ref_types:
        for ref, note in corrections:
            if ref.lower() in {"retractionin", "retractionof"}:
                notes.append(note or ref)
        return "retracted", "; ".join(notes)[:500]
    if ref_types & {"erratumin", "erratumfor", "expressionofconcernin", "expressionofconcernfor"}:
        for ref, note in corrections:
            if ref.lower() in {"erratumin", "erratumfor", "expressionofconcernin", "expressionofconcernfor"}:
                notes.append(note or ref)
        return "correction", "; ".join(notes)[:500]
    if "retraction of publication" in normalized:
        # Отдельная запись-уведомление об отзыве, не сама статья.
        return "correction", "pubmed: retraction notice"
    return "none", ""


def parse_pubmed_xml(xml_text: str) -> list[SourceDocument]:
    """XML от efetch → SourceDocument. Чистая функция (тесты на фикстурах)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceError(f"PubMed XML не разобран: {exc}") from exc

    documents: list[SourceDocument] = []
    for record in root.findall("PubmedArticle"):
        citation = record.find("MedlineCitation")
        if citation is None:
            continue
        article = citation.find("Article")
        if article is None:
            continue
        pmid = _text(citation.find("PMID"))
        if not pmid:
            continue

        title = _text(article.find("ArticleTitle"))
        abstract_parts: list[str] = []
        results_parts: list[str] = []
        for section in article.findall("Abstract/AbstractText"):
            label = (section.get("Label") or "").strip()
            text = _text(section)
            if not text:
                continue
            abstract_parts.append(f"{label}: {text}" if label else text)
            if label.upper() in {"RESULTS", "FINDINGS", "MAIN RESULTS", "RESULTS AND CONCLUSION"}:
                results_parts.append(text)
        abstract = "\n".join(abstract_parts)
        results = "\n".join(results_parts)

        types = publication_types(article)
        corrections: list[tuple[str, str]] = []
        for node in citation.findall("CommentsCorrectionsList/CommentsCorrections"):
            corrections.append((node.get("RefType") or "", _text(node.find("RefSource"))))
        status, retraction_note = retraction_status(types, corrections)

        authors: list[str] = []
        for author in article.findall("AuthorList/Author"):
            collective = author.findtext("CollectiveName")
            if collective:
                authors.append(collective.strip())
                continue
            last = (author.findtext("LastName") or "").strip()
            initials = (author.findtext("Initials") or "").strip()
            if last:
                authors.append(f"{last} {initials}".strip())

        doi = ""
        for node in article.findall("ELocationID"):
            if (node.get("EIdType") or "").lower() == "doi" and _text(node):
                doi = _text(node).strip().lower()
                break
        pmcid = ""
        ids = record.find("PubmedData/ArticleIdList")
        if ids is not None:
            for node in ids.findall("ArticleId"):
                kind = (node.get("IdType") or "").lower()
                if kind == "doi" and not doi:
                    doi = _text(node)
                elif kind == "pmc":
                    pmcid = _text(node)

        year, pub_date = _pub_date(article)
        journal = _text(article.find("Journal/Title"))
        language = _text(article.find("Language"))
        revised = record.findtext("MedlineCitation/DateRevised/Year") or ""

        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        fulltext_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/" if pmcid else (
            f"https://doi.org/{doi}" if doi else url
        )
        content = "\n".join([title, abstract, results])
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        documents.append(
            SourceDocument(
                source="pubmed",
                kind="article",
                external_id=pmid,
                guideline_id=f"pmid-{pmid}",
                title=title,
                revision=f"{revised or year or 'unknown'}#{digest[:12]}",
                text=content,
                abstract=abstract,
                results=results,
                journal=journal,
                year=year,
                authors=tuple(authors),
                doi=doi,
                pmid=pmid,
                pmcid=pmcid,
                url=url,
                fulltext_url=fulltext_url,
                license="publisher terms (abstract only)" if abstract else "",
                language=language,
                publication_types=types,
                retraction=status,
                retraction_note=retraction_note,
                fetched_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                raw={
                    "pub_date": pub_date,
                    "pmid": pmid,
                    "doi": doi,
                    "pmcid": pmcid,
                    "publication_types": list(types),
                    "retraction_status": status,
                },
            )
        )
    return documents


def search_params(cfg, today: dt.date | None = None) -> dict[str, str]:
    today = today or dt.date.today()
    start = today - dt.timedelta(days=max(cfg.days, 1))
    params = {
        "db": "pubmed",
        "term": cfg.query,
        "retmode": "json",
        "retmax": str(max(1, min(cfg.retmax, 500))),
        "datetype": "pdat",
        "mindate": start.strftime("%Y/%m/%d"),
        "maxdate": today.strftime("%Y/%m/%d"),
        "sort": "pub_date",
        "tool": "clinical-compass-kb-enrichment",
    }
    if cfg.email:
        params["email"] = cfg.email
    if cfg.api_key:
        params["api_key"] = cfg.api_key
    return params


def fetch_documents(cfg, today: dt.date | None = None) -> tuple[list[SourceDocument], dict, list[str]]:
    """esearch → efetch. Ошибки транспорта — SourceError; парсинг — построчно."""
    params = search_params(cfg, today)
    search_url = f"{cfg.base_url}/esearch.fcgi?" + urllib.parse.urlencode(params)
    try:
        payload = json.loads(_request(cfg, search_url))
    except json.JSONDecodeError as exc:
        raise SourceError("esearch вернул не JSON") from exc

    result = payload.get("esearchresult", {})
    if "ERROR" in result:
        raise SourceError(f"esearch: {result['ERROR']}")
    ids = [i for i in result.get("idlist", []) if i]
    plan = {
        "source": "pubmed",
        "endpoint": f"{cfg.base_url}/esearch.fcgi",
        "params": {k: v for k, v in params.items() if k != "api_key"},
        "count": int(result.get("count") or 0),
        "returned": len(ids),
    }
    if not ids:
        return [], plan, []

    documents: list[SourceDocument] = []
    errors: list[str] = []
    for start in range(0, len(ids), MAX_BATCH):
        batch = ids[start : start + MAX_BATCH]
        fetch_params = {"db": "pubmed", "retmode": "xml", "id": ",".join(batch)}
        if cfg.email:
            fetch_params["email"] = cfg.email
        if cfg.api_key:
            fetch_params["api_key"] = cfg.api_key
        url = f"{cfg.base_url}/efetch.fcgi?" + urllib.parse.urlencode(fetch_params)
        try:
            documents.extend(parse_pubmed_xml(_request(cfg, url).decode("utf-8", errors="replace")))
        except SourceError as exc:
            errors.append(f"pubmed efetch {batch[0]}..{batch[-1]}: {exc}")
    return documents, plan, errors
