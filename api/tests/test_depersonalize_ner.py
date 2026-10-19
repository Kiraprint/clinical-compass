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
def test_no_network_egress_on_scrub_path(monkeypatch):
    """Гарантия безопасности: весь путь деперсонализации (regex + NER) не делает
    ни одного сетевого вызова — ни сокета, ни DNS, ни HTTP. Это регрессионный
    тест на факт-чек 'pymorphy2 шлёт строки в сервис Яндекса' → неверно:
    pymorphy2 не имеет сетевого кода вовсе, модели natasha упакованы в wheel."""
    import socket as _socket

    attempts = []

    def _boom(what):
        def f(*a, **k):
            attempts.append(what)
            raise AssertionError(f"NETWORK EGRESS BLOCKED: {what} {a} {k}")
        return f

    monkeypatch.setattr(_socket, "socket", _boom("socket()"))
    monkeypatch.setattr(_socket, "create_connection", _boom("create_connection"))
    monkeypatch.setattr(_socket, "getaddrinfo", _boom("getaddrinfo"))

    # regex-путь (флаг выключен) — не должен даже касаться сети
    from app.services.agent import scrub_for_llm

    q, ctx, rep = scrub_for_llm(
        "пациент Иванов, тел +7 999 123-45-67", patient_context=None, names=["Иванов"]
    )
    assert "Иванов" not in q and "[ФИО]" in q and "[ТЕЛЕФОН]" in q
    assert not attempts

    # NER-путь (флаг on) — при наличии natasha тоже полностью оффлайн
    try:
        from app.services.depersonalize import build_name_extractor
    except ImportError:
        build_name_extractor = None
    if build_name_extractor is not None:
        settings.depersonalize_ner = True
        ex = build_name_extractor()
        _ = ex.extract("Проверка оффлайн NER")  # не должно упасть с NETWORK EGRESS
        settings.depersonalize_ner = False
    assert not attempts, f"сетевые попытки: {attempts}"
