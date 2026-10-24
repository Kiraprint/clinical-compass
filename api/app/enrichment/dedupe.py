"""Дедупликация публикаций: DOI → PMID → нормализованный заголовок+год.

Одна и та же работа может прийти из PubMed повторно (обновление записи) или
из другой базы (WHO IRIS / будущий Europe PMC). Ключи нормализуются
детерминированно, поэтому повторный прогон не плодит записи и чанки.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enrichment.types import SourceDocument
from app.models import KBWork

_DOI_PREFIX = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", re.I)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    return _DOI_PREFIX.sub("", doi.strip()).strip().lower()


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title or "").lower().replace("ё", "е")
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def title_key(title: str, year: int | None) -> str:
    return f"{normalize_title(title)}|{year or 'unknown'}"


def keys_for(doc: SourceDocument) -> set[str]:
    """Ключи, по которым документ считается дубликатом уже известного."""
    keys = {f"source:{doc.source}:{doc.external_id}"}
    doi = normalize_doi(doc.doi)
    if doi:
        keys.add(f"doi:{doi}")
    if doc.pmid:
        keys.add(f"pmid:{doc.pmid}")
    if doc.title:
        keys.add(f"title:{title_key(doc.title, doc.year)}")
    return keys


def duplicate_key(doc: SourceDocument, seen: dict[str, str]) -> str | None:
    """Проверка внутри пакета: возвращает ключ, который уже встречался."""
    for key in keys_for(doc):
        if key in seen:
            return key
    return None


def remember(doc: SourceDocument, seen: dict[str, str]) -> None:
    for key in keys_for(doc):
        seen.setdefault(key, doc.external_id)


def _identity_conflict(work: KBWork, doc: SourceDocument) -> bool:
    """Одинаковый title_key у разных работ (перевод/омоним) — не дубликат."""
    meta = work.meta or {}
    work_doi = normalize_doi(meta.get("doi"))
    doc_doi = normalize_doi(doc.doi)
    if work_doi and doc_doi and work_doi != doc_doi:
        return True
    work_pmid = str(meta.get("pmid") or "")
    if work_pmid and doc.pmid and work_pmid != doc.pmid:
        return True
    return False


async def find_existing(db: AsyncSession, doc: SourceDocument) -> KBWork | None:
    """Ищет уже известную работу: источник+id → DOI → title_key.

    title_key — последний резерв и только в пределах одного источника:
    DOI/PMID сравниваются явно, совпавший заголовок с другими идентификаторами
    считается другой работой (иначе возможно слияние разных статей).
    """
    same = await db.scalar(
        select(KBWork).where(
            KBWork.source == doc.source,
            KBWork.external_id == doc.external_id,
        )
    )
    if same is not None:
        return same

    doi = normalize_doi(doc.doi)
    if doi:
        by_doi = await db.scalar(select(KBWork).where(KBWork.meta["doi"].astext == doi))
        if by_doi is not None:
            return by_doi

    if doc.title:
        by_title = await db.scalar(
            select(KBWork).where(
                KBWork.meta["title_key"].astext == title_key(doc.title, doc.year),
                KBWork.source == doc.source,
            )
        )
        if by_title is not None and not _identity_conflict(by_title, doc):
            return by_title
    return None
