"""NER-расширение деперсонализации: условная библиотека, деградация, флаг (docs/12)."""

import pytest

from app.config import settings
from app.services.depersonalize import (
    NullNameExtractor,
    build_name_extractor,
)


@pytest.fixture(autouse=True)
def _reset_extractor_cache():
    """build_name_extractor кеширует экземпляр на процесс — сбрасываем между тестами."""
    import app.services.depersonalize as mod

    before = mod._extractor
    mod._extractor = None
    yield
    mod._extractor = before


def test_flag_off_uses_null():
    settings.depersonalize_ner = False
    assert isinstance(build_name_extractor(), NullNameExtractor)


def test_fallback_without_natasha(monkeypatch):
    """Флаг on, но natasha не установлен → деградация до regex, без падения.

    (Проверяем поведение, не лог: caplog-перехват в conftest-окружении
    ненадёжен — логгер app.* тонет до root при модульном импорте на коллекции.)
    """
    settings.depersonalize_ner = True
    monkeypatch.delitem(
        __import__("sys").modules, "natasha", raising=False
    )  # гарантируем отсутствие
    # importlib не нашёл natasha → build() бросит ModuleNotFoundError → extract []
    from app.services.depersonalize import NatashaNameExtractor

    ex = NatashaNameExtractor()
    ex._pipeline = None
    assert ex.extract("Иван Иванов пришёл") == []  # нет падения, нет ложных имён
    # деградация одноразовая: вторая попытка не падает тоже
    assert ex.extract("ещё текст") == []


def test_fake_natasha_module_extracts_per(monkeypatch):
    """С фейковым natasha (PER-спаны) имена попадают в результат."""
    import sys
    import types

    class _Span:
        def __init__(self, text, kind):
            self.text = text
            self.type = kind

        def normalize(self, morph):
            pass

    class _Doc:
        def __init__(self, text):
            self.text = text
            self.spans = [
                _Span("Иванов Иван", "PER"),
                _Span("Москва", "LOC"),
                _Span("Иванов Иван", "PER"),  # дубль отфильтруется
            ]

        def segment(self, seg):
            pass

        def tag_ner(self, ner):
            pass

    fake = types.ModuleType("natasha")

    def _Doc_factory(text):
        return _Doc(text)

    fake.Doc = _Doc_factory
    fake.MorphVocab = lambda: object()
    fake.NewsEmbedding = lambda: object()
    fake.NewsNERTagger = lambda emb: object()
    fake.Segmenter = lambda: object()
    sys.modules["natasha"] = fake
    monkeypatch.setattr(sys, "modules", {**sys.modules, "natasha": fake})
    try:
        from app.services.depersonalize import NatashaNameExtractor

        ex = NatashaNameExtractor()
        assert ex.extract("Иванов Иван в Москве") == ["Иванов Иван"]
    finally:
        sys.modules.pop("natasha", None)


def test_scrub_for_llm_uses_ner_names(monkeypatch):
    """NER-имена доливаются в deny-list: маскируются и в падеже, без ростра."""
    from app.services.agent import scrub_for_llm

    settings.depersonalize_ner = True

    import app.services.depersonalize as mod

    class _NER:
        def extract(self, text):
            return ["Сидоров Пётр"]

    mod._extractor = _NER()
    query, ctx, report = scrub_for_llm(
        "к Сидорову Петру заходить", patient_context=None, names=[]
    )
    assert "[ФИО]" in query
    assert "Сидоров" not in query
    assert "FIO" in report["masked"]


def test_scrub_for_llm_without_ner_keeps_default(monkeypatch):
    """Флаг off: NER не вызывается, deny-list прежний."""
    from app.services.agent import scrub_for_llm

    settings.depersonalize_ner = False

    import app.services.depersonalize as mod

    called = []

    class _NER:
        def extract(self, text):
            called.append(text)
            return ["Сидоров Пётр"]

    mod._extractor = _NER()
    query, _, report = scrub_for_llm(
        "к Сидорову Петру заходить", patient_context=None, names=[]
    )
    assert called == []
    assert query == "к Сидорову Петру заходить"
    assert report["applied"] is False