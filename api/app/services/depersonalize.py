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


class NullNameExtractor:
    """NER выключен: только deny-list + regex (детерминированное демо)."""

    def extract(self, text: str) -> list[str]:
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
        names = []
        for span in doc.spans:
            if getattr(span, "type", None) != "PER":
                continue
            if morph is not None:
                try:
                    span.normalize(morph)
                except Exception:
                    pass  # нормализация опциональна; маскируем исходное написание
            name = span.text.strip()
            if name and name not in names:
                names.append(name)
        return names


_extractor = None


def build_name_extractor() -> NameExtractor:
    """Один экземпляр на процесс (сегментер/модели грузятся один раз)."""
    global _extractor
    if _extractor is None:
        _extractor = NatashaNameExtractor() if settings.depersonalize_ner else NullNameExtractor()
    return _extractor