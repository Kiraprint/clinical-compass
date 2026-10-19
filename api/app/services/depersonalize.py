"""NER-расширение деперсонализации (docs/12): ФИО-детекция сверх deny-list.

SPI: NameExtractor.extract(text) -> list[str] — имена для доливки в deny-list
деперсонализатора. Штатно — NullNameExtractor (чистый regex-путь, детерминизм
демо). При CC_DEPERSONALIZE_NER=true и установленном natasha — NatashaNameExtractor.

natasha — опциональная зависимость (extra "ner" в pyproject): импорт ленивый,
при отсутствии/сбое моделей деградация до regex-пути с логом, без падения.
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.config import settings

logger = logging.getLogger(__name__)


class NameExtractor(Protocol):
    def extract(self, text: str) -> list[str]:
        """Имена (в исходном написании) для маскирования в text."""
        ...

    def extract_spans(self, text: str) -> list[tuple[str, int, int]]:
        """(вид, start, end) найденных сущностей: FIO/ADDRESS/DOC/PHONE/EMAIL.

        Нужен бенчмарку (bench/ner) для честного сравнения кандидатов и
        будущему прямому маскированию по спанам.
        """
        ...


class NullNameExtractor:
    """NER выключен: только deny-list + regex (детерминированное демо)."""

    def extract(self, text: str) -> list[str]:
        return []

    def extract_spans(self, text: str) -> list[tuple[str, int, int]]:
        return []


class NatashaNameExtractor:
    """natasha NewsNERTagger: теги PER → имена в deny-list.

    Конвейер строится лениво при первом вызове; при недоступности библиотеки
    или моделей — лог + деградация до Null (запросы продолжают работать).
    """

    def __init__(self) -> None:
        self._pipeline = None  # None = ещё не инициализирован/недоступен

    def _build(self):
        """(lazy) сегментер + NER-таггер natasha. Бросает ImportError/OSError.

        MorphVocab (pymorphy2) опционален: на Python 3.14 его словари через
        pkg_resources-энтрипойнты не грузятся — нормализацию спана пропускаем,
        маскируем исходное написание (для deny-list этого достаточно).
        """
        from natasha import NewsEmbedding, NewsNERTagger, Segmenter

        seg = Segmenter()
        ner = NewsNERTagger(NewsEmbedding())
        morph = None
        try:
            from natasha import MorphVocab

            morph = MorphVocab()
        except Exception:  # pymorphy2 может отсутствовать/быть сломан — не критично
            pass
        return seg, morph, ner

    def extract(self, text: str) -> list[str]:
        return [text[s:e] for kind, s, e in self.extract_spans(text) if kind == "FIO"]

    def extract_spans(self, text: str) -> list[tuple[str, int, int]]:
        if self._pipeline is None:
            try:
                self._pipeline = self._build()
            except Exception as exc:  # ImportError, OSError (модели), любые сбои инициализации
                logger.warning(
                    "depersonalize_ner=true, но natasha недоступен (%s: %s) — "
                    "деградация до regex/deny-list",
                    type(exc).__name__, exc,
                )
                self._pipeline = False  # больше не пробуем в этом процессе
        if self._pipeline is False or not text:
            return []
        seg, morph, ner = self._pipeline

        from natasha import Doc

        doc = Doc(text)
        doc.segment(seg)
        try:
            doc.tag_ner(ner)
        except Exception as exc:
            logger.warning("natasha NER сбой на фрагменте: %s", exc)
            return []
        out: list[tuple[str, int, int]] = []
        for span in doc.spans:
            kind = _NATASHA_KIND.get(getattr(span, "type", ""))
            if kind is None:
                continue
            start, stop = span.start, span.stop
            if stop > start:
                out.append((kind, start, stop))
        return out


# natasha-типы: PER -> ФИО, LOC -> адрес (грубо; бенчмарк bench/ner это измеряет).
_NATASHA_KIND = {"PER": "FIO", "LOC": "ADDRESS"}

class RubertPiiNameExtractor:
    """redmadrobot-rnd/rubert-base-pii-ner: RU-PII token-classification.

    Apache-2.0, обучен на русских ПДн (ФИО/адреса/документы/контакты).
    На redmadrobot-rnd/pii_benchmark FIO F1 0.933 exact / 0.947 overlap —
    в 5.5x точнее natasha (0.168/0.456), см. bench/ner/README.md.

    Зависимости опциональны (extra "ner-rubert"): torch + transformers.
    При их отсутствии/сбое модели — лог + деградация до regex-пути.
    """

    def __init__(self) -> None:
        self._pipe = None

    def _build(self):
        import torch
        from transformers import pipeline

        device = 0 if torch.cuda.is_available() else -1
        return pipeline(
            "token-classification",
            model=settings.ner_model,
            device=device,
            aggregation_strategy="first",
        )

    def extract_spans(self, text: str) -> list[tuple[str, int, int]]:
        if self._pipe is None:
            try:
                self._pipe = self._build()
            except Exception as exc:  # ImportError/OSError/любой сбой инициализации
                logger.warning(
                    "depersonalize_ner=rubert, но модель недоступна (%s: %s) — "
                    "деградация до regex/deny-list",
                    type(exc).__name__, exc,
                )
                self._pipe = False
        if self._pipe is False or not text:
            return []
        try:
            ents = self._pipe(text)
        except Exception as exc:
            logger.warning("rubert NER сбой на фрагменте: %s", exc)
            return []
        out: list[tuple[str, int, int]] = []
        for e in ents:
            label = e.get("entity_group") or e.get("entity")
            kind = _RUBERT_KIND.get(label)
            if kind and int(e["end"]) > int(e["start"]):
                out.append((kind, int(e["start"]), int(e["end"])))
        return out

    def extract(self, text: str) -> list[str]:
        return [text[s:e] for kind, s, e in self.extract_spans(text) if kind == "FIO"]


# Категории rubert-base-pii-ner -> виды нашего гварда.
_RUBERT_KIND = {
    "FIRST_NAME": "FIO", "LAST_NAME": "FIO", "MIDDLE_NAME": "FIO", "NAME": "FIO",
    "CITY": "ADDRESS", "STREET": "ADDRESS", "HOUSE": "ADDRESS",
    "REGION": "ADDRESS", "DISTRICT": "ADDRESS", "COUNTRY": "ADDRESS",
    "PASSPORT": "DOC", "SNILS": "DOC", "INN": "DOC", "OMS": "DOC",
    "DRIVER_LICENSE": "DOC", "MILITARY_ID": "DOC", "BIRTH_CERTIFICATE": "DOC",
    "CREDIT_CARD": "DOC", "PHONE": "PHONE", "EMAIL": "EMAIL",
}

_NER_OFF = {"", "off", "false", "0", "none", "no"}


def ner_backend() -> str:
    """Нормализованное имя backend'а: off | natasha | rubert."""
    raw = settings.depersonalize_ner
    if isinstance(raw, bool):  # обратная совместимость (тесты/старые конфиги)
        return "natasha" if raw else "off"
    value = str(raw).strip().lower()
    if value in _NER_OFF:
        return "off"
    if value in ("true", "1", "yes", "on", "natasha"):
        return "natasha"
    if value == "rubert":
        return "rubert"
    logger.warning("неизвестный CC_DEPERSONALIZE_NER=%r — NER выключен", raw)
    return "off"


_extractor = None


def build_name_extractor() -> NameExtractor:
    """Один экземпляр на процесс (модели грузятся один раз)."""
    global _extractor
    if _extractor is None:
        backend = ner_backend()
        if backend == "rubert":
            _extractor = RubertPiiNameExtractor()
        elif backend == "natasha":
            _extractor = NatashaNameExtractor()
        else:
            _extractor = NullNameExtractor()
    return _extractor
