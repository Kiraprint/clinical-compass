"""Деперсонализация: структурные форматы, спаны, fail-closed, egress (docs/12, docs/10)."""

from __future__ import annotations

import types

import pytest

from app.config import settings
from app.services import chat_insights
from app.services.depersonalize import (
    DepersonalizationUnavailable,
    RubertPiiNameExtractor,
    scrub_text,
)

DOC = "[ДОКУМЕНТ]"
PHONE = "[ТЕЛЕФОН]"
FIO = "[ФИО]"


@pytest.fixture(autouse=True)
def _restore_settings():
    prod, ner = settings.prod_mode, settings.depersonalize_ner
    yield
    settings.prod_mode, settings.depersonalize_ner = prod, ner


@pytest.fixture
def fake_extractor(monkeypatch):
    """Подмена ML-backend'а: спаны задаются тестом (модель не грузится)."""
    import app.services.depersonalize as mod

    class _Extractor:
        def __init__(self, spans=()):
            self.spans = list(spans)
            self.seen = []

        def extract(self, text):
            return []

        def extract_spans(self, text):
            self.seen.append(text)
            return list(self.spans)

    ex = _Extractor()
    monkeypatch.setattr(mod, "_extractor", ex)
    return ex


def _valid_snils(prefix="112233445") -> str:
    from compass.depersonalize import snils_checksum

    c = snils_checksum(prefix)
    return f"{prefix[:3]}-{prefix[3:6]}-{prefix[6:9]} {c:02d}"


def _valid_oms(base="123456789012345") -> str:
    checksum = sum(int(c) for c in base[0::2]) % 10
    return base + str(checksum)


# --------------------------------------------------------- структурные форматы

@pytest.mark.parametrize("text", [
    "+7 926 123-45-67",
    "8 (926) 123-45-67",
    "89261234567",
    "+79261234567",
    "8 926 123 45 67",
    "926 123-45-67",
    "9261234567",
    "8-926-123-45-67",
])
def test_phone_forms_masked(text):
    r = scrub_text(text)
    assert r.text == PHONE, text
    assert r.findings[0].kind == "PHONE"


@pytest.mark.parametrize("text", [
    "СНИЛС 123-456-789 01",
    f"СНИЛС {_valid_snils()}",
    "снилс: 11223344566",
])
def test_snils_forms_masked(text):
    r = scrub_text(text)
    assert DOC in r.text, text
    assert r.findings[-1].category == "SNILS"


@pytest.mark.parametrize("text,category", [
    ("ИНН 7707083893", "INN"),
    ("ИНН 500100732259", "INN"),
    (f"полис {_valid_oms()}", "OMS"),
    ("полис 1234 5678 9012 3456", "OMS"),
    ("паспорт 4509 123456", "PASSPORT"),
    ("паспорт 4509123456", "PASSPORT"),
    ("Медкарта № 456123", "MEDCARD"),
    ("ИБ № 45678/2024", "MEDCARD"),
    ("история болезни 987654", "MEDCARD"),
    ("медкарта № 77-004521", "MEDCARD"),
])
def test_document_forms_masked(text, category):
    r = scrub_text(text)
    assert DOC in r.text, text
    assert r.findings[-1].category == category


@pytest.mark.parametrize("text", [
    "дата рождения 12.03.1954",
    "д.р. 12-03-1954",
    "12/03/1954 г.р.",
    "родился 1985-04-17",
    "дата рождения 9 августа 1949",
])
def test_dob_forms_masked(text):
    r = scrub_text(text)
    assert "[ДАТА РОЖДЕНИЯ]" in r.text, text
    assert r.findings[-1].kind == "DOB"


@pytest.mark.parametrize("text", [
    "г. Москва, ул. Арбат, д. 21, кв. 8",
    "Санкт-Петербург, Лиговский пр-т, д. 55, кв. 17",
    "Московская обл., г. Химки, ул. Мира, 12, кв. 45",
    "пер. Тупой, д. 2, кв. 6",
])
def test_address_forms_masked(text):
    r = scrub_text(text)
    assert r.text == "[АДРЕС]", text


def test_clinical_dates_and_alone_age_not_masked():
    for text in ("приём 12.03.2024", "АД 160/90, ЧДД 18", "пациент 64 года"):
        assert scrub_text(text).text == text, text



def test_age_masked_only_with_fio_span(fake_extractor):
    settings.depersonalize_ner = "hybrid"
    text = "Пациентка Петрова Анна Ивановна, 64 года"
    start = text.index("Петрова")
    fake_extractor.spans = [("FIO", start, start + len("Петрова Анна Ивановна"))]
    r = scrub_text(text)
    assert FIO in r.text and "[ВОЗРАСТ]" in r.text
    fake_extractor.spans = []
    # ФИО ловит структурная эвристика (docs/12), поэтому возраст в связке
    # маскируется и без спана модели.
    plain = scrub_text(text)
    assert FIO in plain.text and "[ВОЗРАСТ]" in plain.text
    # Одиночный возраст без ФИО — клинический показатель, не ПДн.
    assert scrub_text("Пациентке 64 года").text == "Пациентке 64 года"


# --------------------------------------------------------- спаны и перекрытия

def test_overlapping_spans_produce_no_duplicates(fake_extractor):
    settings.depersonalize_ner = "hybrid"
    text = "Меня зовут Иванов Пётр Сергеевич, звоните +79261234567"
    start = text.index("Иванов")
    fake_extractor.spans = [
        ("FIO", start, start + len("Иванов")),               # частичное
        ("FIO", start, start + len("Иванов Пётр")),          # ещё частичное
        ("FIO", start, start + len("Иванов Пётр Сергеевич")),
    ]
    r = scrub_text(text)
    assert r.text.count(FIO) == 1
    assert "Иванов" not in r.text
    assert r.text.endswith(PHONE)


def test_span_outside_text_ignored(fake_extractor):
    settings.depersonalize_ner = "hybrid"
    fake_extractor.spans = [("FIO", 100, 200)]
    assert scrub_text("короткий текст").text == "короткий текст"


# --------------------------------------------------------- fail-closed (prod)

def test_prod_fail_closed_does_not_call_llm(monkeypatch, fake_extractor):
    """CC_PROD_MODE + ML-backend не загрузился → 503, LLM не вызывается."""
    import app.services.depersonalize as mod
    from app.services import llm_router
    from app.services.agent import scrub_for_llm

    settings.prod_mode = True
    settings.depersonalize_ner = "rubert"
    monkeypatch.setattr(mod, "_extractor", None)

    def _boom(self):
        raise ImportError("torch not installed")

    monkeypatch.setattr(RubertPiiNameExtractor, "_build", _boom)
    calls = []
    monkeypatch.setattr(llm_router, "chat_completion",
                        lambda *a, **k: calls.append(a) or {"choices": []})

    with pytest.raises(DepersonalizationUnavailable) as err:
        scrub_for_llm("Меня зовут Иванов Пётр Сергеевич", None, names=[])
    assert err.value.status == 503
    assert err.value.code == "DEPERSONALIZATION_UNAVAILABLE"
    assert calls == [], "сырой текст не должен уходить в LLM"


def test_prod_inference_failure_is_fail_closed(monkeypatch):
    """Модель загрузилась, но инференс упал — тоже 503, без regex-деградации."""
    import app.services.depersonalize as mod

    settings.prod_mode = True
    settings.depersonalize_ner = "rubert"

    class _BrokenPipe:
        def __call__(self, text):
            raise RuntimeError("CUDA OOM")

    ex = RubertPiiNameExtractor()
    ex._pipe = _BrokenPipe()
    monkeypatch.setattr(mod, "_extractor", ex)
    with pytest.raises(DepersonalizationUnavailable):
        mod.extract_pii_spans("Иванов Иван")


def test_non_prod_degrades_to_regex(monkeypatch):
    """Вне prod недоступная модель — лог + regex-путь, запрос не падает."""
    import app.services.depersonalize as mod

    settings.prod_mode = False
    settings.depersonalize_ner = "rubert"
    monkeypatch.setattr(mod, "_extractor", None)
    monkeypatch.setattr(RubertPiiNameExtractor, "_build",
                        lambda self: (_ for _ in ()).throw(ImportError("no torch")))
    r = scrub_text("Иванов Иван, +79261234567")
    assert r.text.endswith(PHONE)
    # Без ML-модели ФИО всё равно маскируется структурной эвристикой ядра
    # (docs/12, правило фамилия+имя), а не уходит открытым текстом.
    assert "Иванов" not in r.text
    assert FIO in r.text


def test_unknown_fio_masked_with_prod_backend(fake_extractor):
    """Регресс живого инцидента: неизвестное ростру ФИО маскируется NER-спаном."""
    from app.services.agent import scrub_for_llm

    settings.prod_mode = True
    settings.depersonalize_ner = "hybrid"
    text = "Меня зовут Иванов Пётр Сергеевич, звоните +79261234567"
    start = text.index("Иванов")
    fake_extractor.spans = [("FIO", start, start + len("Иванов Пётр Сергеевич"))]
    query, _, report = scrub_for_llm(text, None, names=[])
    assert "Иванов" not in query and FIO in query and PHONE in query
    assert report["masked"]["FIO"] == 1


# --------------------------------------------------------- chat_insights (FR-31)

def _msg(sender: str, content: str):
    return types.SimpleNamespace(sender=sender, content=content)


def test_dialogue_only_patient_messages_chronological():
    msgs = [
        _msg("SYSTEM", "Помощник: Спасибо, что поделились. Это важно."),
        _msg("CLINICIAN", "Врач: диктовка осмотра, АД 150/90"),
        _msg("PATIENT", "Здравствуйте, меня зовут Иванов Пётр"),
        _msg("PATIENT", "Температура 38, пью ибупрофен"),
    ]
    # names=[] — deny-list пуст; маскирование структурных ПДн идёт внутри.
    dialogue = chat_insights._dialogue_text(msgs, [])
    assert "Помощник" not in dialogue and "диктовка" not in dialogue
    assert "Иванов" not in dialogue
    assert dialogue.index(FIO) < dialogue.index("Температура")


def test_insights_prompt_has_no_phi_and_no_noise(monkeypatch, fake_extractor):
    settings.depersonalize_ner = "hybrid"
    text = "Меня зовут Иванов Пётр Сергеевич, пью ибупрофен"
    start = text.index("Иванов")
    fake_extractor.spans = [("FIO", start, start + len("Иванов Пётр Сергеевич"))]

    # scrub уже внутри _dialogue_text: до LLM доходит маскированная лента.
    dialogue = chat_insights._dialogue_text([
        _msg("SYSTEM", "Помощник: Спасибо, что поделились."),
        _msg("PATIENT", text),
    ], [])
    sent: list[dict] = []
    monkeypatch.setattr(
        chat_insights.llm_router, "chat_completion",
        lambda messages, **kw: sent.append(messages) or {
            "choices": [{"message": {"content": "[]"}}]
        },
    )
    assert chat_insights._ask_llm(dialogue) == []
    user_prompt = sent[0][-1]["content"]
    assert "Иванов" not in user_prompt
    assert "Помощник" not in user_prompt
    assert "Спасибо, что поделились" not in user_prompt


def test_insights_timeout_retried_once(monkeypatch, caplog):
    calls = []

    def _flaky(messages, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return {"choices": [{"message": {"content": "[]"}}]}

    monkeypatch.setattr(chat_insights.llm_router, "chat_completion", _flaky)
    with caplog.at_level("WARNING"):
        assert chat_insights._ask_llm("Пациент: тест") == []
    assert len(calls) == 2
    assert all(c.get("timeout") == settings.chat_insights_timeout for c in calls)
    assert "timeout" in caplog.text.lower()


def test_insights_timeout_twice_raises_503(monkeypatch):
    from app.errors import Problem

    def _always_timeout(messages, **kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(chat_insights.llm_router, "chat_completion", _always_timeout)
    with pytest.raises(Problem) as err:
        chat_insights._ask_llm("Пациент: тест")
    assert err.value.code == "CHAT_INSIGHTS_UNAVAILABLE"


# --------------------------------------------------------- assistant_chat egress

def test_assistant_chat_history_uses_hardened_scrub(monkeypatch):
    from app.services import assistant_chat

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _DB:
        async def scalars(self, _q):
            return _Rows([
                types.SimpleNamespace(role="assistant", content="ответ"),
                types.SimpleNamespace(role="user", content="Иванов Пётр Сергеевич"),
            ])

    seen = []

    def _fake_scrub(text, ctx, names):
        seen.append((text, names))
        return ("[ФИО]", None, {})

    monkeypatch.setattr(assistant_chat, "scrub_for_llm", _fake_scrub)
    history = __import__("asyncio").run(
        assistant_chat._history_for_llm(_DB(), "thread-id", ["Иванов"])
    )
    assert history == [
        {"role": "user", "content": "[ФИО]"},
        {"role": "assistant", "content": "[ФИО]"},
    ]
    assert [t for t, _ in seen] == ["Иванов Пётр Сергеевич", "ответ"]
