"""Конфигурация обогащения (env CC_ENRICH_*).

Отдельный класс от app.config.Settings: эти параметры нужны только сервису
kb-enrichment (у API нет исходящего трафика в PubMed/ВОЗ).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.config import settings
from app.enrichment.who import WHO_HANDLES, WHOHandle

# Продуктовый скоуп (docs/13 §2): тревога/депрессия + коморбидность, ТРД,
# БАР, суицидальный риск. Только систематические обзоры и метаанализы:
# они и есть «метаисследования» из запроса.
DEFAULT_QUERY = (
    '('
    '"Depressive Disorder"[MeSH] OR "Depression"[MeSH] OR "Anxiety Disorders"[MeSH] '
    'OR "Bipolar Disorder"[MeSH] OR "Suicide"[MeSH] '
    'OR depression[tiab] OR anxiety[tiab] OR bipolar[tiab] '
    'OR "treatment-resistant depression"[tiab]'
    ') AND ("Systematic Review"[pt] OR "Meta-Analysis"[pt]) '
    'AND (english[la] OR russian[la]) AND hasabstract'
)


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PubMedConfig:
    enabled: bool = True
    query: str = DEFAULT_QUERY
    days: int = 30
    retmax: int = 100
    email: str = ""
    api_key: str = ""
    base_url: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    timeout: float = 60.0
    user_agent: str = "clinical-compass-kb-enrichment/1.0"


@dataclass(frozen=True)
class WHOConfig:
    enabled: bool = True
    handles: tuple[WHOHandle, ...] = WHO_HANDLES
    base_url: str = "https://iris.who.int/server/api"
    max_text_chars: int = 4_000_000
    timeout: float = 60.0
    user_agent: str = "clinical-compass-kb-enrichment/1.0"


@dataclass(frozen=True)
class EnrichmentConfig:
    pubmed: PubMedConfig = field(default_factory=PubMedConfig)
    who: WHOConfig = field(default_factory=WHOConfig)
    offline: bool = False
    auto_publish_guidelines: bool = True
    timeout: float = 60.0
    interval_hours: float = 24.0
    run_on_start: bool = True
    chunk_chars: int = 1800
    chunk_overlap: int = 120
    user_agent: str = "clinical-compass-kb-enrichment/1.0 (evidence ingest; contact: team)"

    @classmethod
    def from_env(cls) -> "EnrichmentConfig":
        email = _env_str("CC_ENRICH_PUBMED_EMAIL", "")
        timeout = float(_env_int("CC_ENRICH_TIMEOUT", 60))
        agent = f"clinical-compass-kb-enrichment/1.0 (evidence ingest; contact: {email})" if email else (
            "clinical-compass-kb-enrichment/1.0 (evidence ingest)"
        )
        return cls(
            pubmed=PubMedConfig(
                enabled=_env_bool("CC_ENRICH_PUBMED_ENABLED", True),
                query=_env_str("CC_ENRICH_PUBMED_QUERY", DEFAULT_QUERY),
                days=_env_int("CC_ENRICH_PUBMED_DAYS", 30),
                retmax=_env_int("CC_ENRICH_PUBMED_RETMAX", 100),
                email=email,
                api_key=_env_str("CC_ENRICH_PUBMED_API_KEY", ""),
                base_url=_env_str(
                    "CC_ENRICH_PUBMED_BASE_URL",
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
                ),
                timeout=timeout,
                user_agent=agent,
            ),
            who=WHOConfig(
                enabled=_env_bool("CC_ENRICH_WHO_ENABLED", True),
                max_text_chars=_env_int("CC_ENRICH_WHO_MAX_TEXT_CHARS", 4_000_000),
                base_url=_env_str("CC_ENRICH_WHO_BASE_URL", "https://iris.who.int/server/api"),
                timeout=timeout,
                user_agent=agent,
            ),
            offline=_env_bool("CC_ENRICH_OFFLINE", False),
            auto_publish_guidelines=_env_bool("CC_ENRICH_AUTO_PUBLISH_GUIDELINES", True),
            timeout=timeout,
            interval_hours=float(_env_int("CC_ENRICH_INTERVAL_HOURS", 24)),
            run_on_start=_env_bool("CC_ENRICH_RUN_ON_START", True),
            # Дефолты — общие с сидом (CC_KB_CHUNK_*, docs/08 §3), чтобы
            # гайдлайны ВОЗ и остальная КБ резались одинаково; CC_ENRICH_CHUNK_*
            # остаётся точечным переопределением для обогащения.
            chunk_chars=_env_int("CC_ENRICH_CHUNK_CHARS", settings.kb_chunk_chars),
            chunk_overlap=_env_int("CC_ENRICH_CHUNK_OVERLAP", settings.kb_chunk_overlap),
            user_agent=agent,
        )
