"""Извлечение ключевых сведений из клинической истории (метрика кейса 2).

Детерминированный парсер без LLM (красные линии docs/01: структурирование, не
диагноз). Поддерживает разные шаблоны: различный порядок разделов, стиль описания,
формат фиксации диагнозов (МКБ-10) и назначений.

Результат — ЧЕРНОВИК для подтверждения врачом, не авто-запись в клиническую модель.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExtractedHistory:
    complaints: str | None = None
    anamnesis: str | None = None
    diagnosis_codes: tuple[str, ...] = ()
    therapy: tuple[str, ...] = ()
    scale_scores: tuple[str, ...] = ()
    template: str = "unknown"
    fields_found: int = 0


# Синонимы заголовков разделов (RU/EN, разные шаблоны).
_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "complaints": ("жалобы", "complaints", "жалобы при поступлении"),
    "anamnesis": ("анамнез", "anamnesis", "анамнез заболевания", "анамнез жизни", "история заболевания"),
    "diagnosis": ("диагноз", "diagnosis", "заключение", "клинический диагноз"),
    "therapy": ("лечение", "therapy", "назначено", "назначения", "терапия", "принимает"),
}

# МКБ-10: F00-F99 (психические расстройства) — F41.1, F32.1, F43.2 и т.п.
_ICD10_RE = re.compile(r"\bF\d{2}(?:\.\d{1,2})?\b")

# Препараты: слово + дозировка (мг/мл/г). Имена могут быть со строчной («гидроксизин»);
# служебные слова-глаголы исключаются стоп-листом.
_DRUG_RE = re.compile(
    r"\b([А-ЯЁA-Zа-яёa-z][а-яёa-z\-]{2,}(?:®)?)\s*[—-]?\s*(\d+(?:[.,]\d+)?\s*(?:мг|мл|г))\b",
)
_DRUG_STOPWORDS = {
    "принимает", "принимать", "назначено", "назначен", "терапия", "лечение",
    "доза", "дозе", "дозу", "по", "в", "и", "или", "сутки", "сут", "раз",
}

# Баллы шкал: «PHQ-9 = 12», «GAD-7: 8», «HADS 10».
_SCALE_RE = re.compile(r"\b(PHQ-?9|GAD-?7|HADS(?:-?[AD])?)\s*[:=—\-]?\s*(\d{1,2})\b", re.I)


def _section_of(line: str) -> tuple[str, str] | None:
    """Определяет раздел по заголовку строки. Возвращает (раздел, остаток строки).

    Заголовок распознаётся как префикс строки (до ':' / '—' / '–' или сразу текст):
    поддерживает «Диагноз: F41.1», «Диагноз — F41.1», «Анамнез заболевания: …».
    Алиасы перебираются от длинных к коротким, чтобы «анамнез заболевания» не
    перехватывался «анамнезом».
    """
    low = line.lower()
    pairs = sorted(
        ((alias, key) for key, aliases in _SECTION_ALIASES.items() for alias in aliases),
        key=lambda p: len(p[0]),
        reverse=True,
    )
    for alias, key in pairs:
        if low.startswith(alias):
            rest = line[len(alias):].lstrip(" \t:—–-")
            return key, rest.strip()
    return None


def extract_history(text: str, template_name: str | None = None) -> ExtractedHistory:
    """Извлекает поля истории. Порядок разделов не важен — парсим по заголовкам."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        hit = _section_of(line)
        if hit is not None:
            current, rest = hit
            sections.setdefault(current, [])
            if rest:
                sections[current].append(rest)
            continue
        if current is not None:
            sections[current].append(line)

    def _join(key: str) -> str | None:
        parts = sections.get(key)
        if not parts:
            return None
        return " ".join(parts).strip() or None

    diagnosis_text = " ".join(sections.get("diagnosis", []))
    therapy_text = " ".join(sections.get("therapy", []))

    codes = tuple(dict.fromkeys(_ICD10_RE.findall(diagnosis_text)))
    drugs = tuple(
        dict.fromkeys(
            f"{m.group(1)} {m.group(2)}"
            for m in _DRUG_RE.finditer(therapy_text)
            if m.group(1).lower() not in _DRUG_STOPWORDS
        )
    )
    scales = tuple(dict.fromkeys(f"{m.group(1).upper()} = {m.group(2)}" for m in _SCALE_RE.finditer(text)))

    found = sum(1 for v in (_join("complaints"), _join("anamnesis"), diagnosis_text or None,
                            therapy_text or None, scales or None) if v)

    return ExtractedHistory(
        complaints=_join("complaints"),
        anamnesis=_join("anamnesis"),
        diagnosis_codes=codes,
        therapy=drugs,
        scale_scores=scales,
        template=template_name or _detect_template(sections),
        fields_found=found,
    )


def _detect_template(sections: dict[str, list[str]]) -> str:
    """Грубая эвристика шаблона по набору разделов (для метрики покрытия)."""
    keys = set(sections)
    if {"complaints", "anamnesis", "diagnosis", "therapy"} <= keys:
        return "full"
    if {"diagnosis", "therapy"} <= keys:
        return "brief"
    return "partial"


__all__ = ["ExtractedHistory", "extract_history"]
