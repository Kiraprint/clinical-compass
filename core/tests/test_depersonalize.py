"""Тесты деперсонализации (docs/12). Чистая функция из ядра, без I/O."""

import pytest

from compass.depersonalize import (
    ADDRESS,
    DOB,
    DOC,
    EMAIL,
    FIO,
    PHONE,
    depersonalize,
)


def test_phone_masked():
    for text in (
        "+7 999 123-45-67",
        "8 900 123 45 67",
        "+7(999)123-45-67",
        "89991234567",
    ):
        r = depersonalize(text)
        assert r.text == "[ТЕЛЕФОН]", text
        assert r.findings[0].kind == PHONE


def test_email_masked_including_cyrillic_local():
    r = depersonalize("пишите на doctor@clinic.org")
    assert r.text == "пишите на [EMAIL]"
    r2 = depersonalize("контакт иванов@демо.рф")
    assert "[EMAIL]" in r2.text
    assert "иванов@" not in r2.text


def test_document_numbers_masked():
    cases = (
        "СНИЛС 123-456-789 01",
        "ИНН 1234567890",
        "ИНН 123456789012",
        "полис 1234567890123456",
        "паспорт 4509 123456",
    )
    for text in cases:
        r = depersonalize(text)
        assert r.text.endswith("[ДОКУМЕНТ]"), text
        assert r.findings[-1].kind == DOC


def test_date_of_birth_masked_with_marker():
    """Дата рождения маскируется только с маркером (docs/12); клинические даты
    без маркера не трогаем, чтобы не портить смысл запроса."""
    for text in (
        "дата рождения 12.03.1954",
        "Дата рождения: 12-03-1954",
        "д.р. 12.03.1954",
        "род. 12/03/1954",
    ):
        r = depersonalize(text)
        assert r.text.endswith("[ДАТА РОЖДЕНИЯ]"), text
        assert r.findings[-1].kind == DOB
    # Без маркера дата остаётся: это может быть дата приёма/начала симптомов.
    assert depersonalize("приём 12.03.1954").text == "приём 12.03.1954"

def test_fio_deny_list_variants():
    names = ["Иванов Иван Иванович"]
    for text in (
        "пациент Иванов Иван Иванович",
        "пациент Иванов И. И.",
        "пациент Иванов И.И.",
        "пациент Иванов Иван",
        "пациент Иванов",
        "пациент иванов ИВАН ИВАНОВИЧ",  # регистр
    ):
        r = depersonalize(text, names=names)
        assert r.text == "пациент [ФИО]", text
        assert r.findings[0].kind == FIO


def test_fio_short_surname_not_masked_alone():
    # «К.» — инициал, не фамилия: отдельно не маскируем, но полное имя маскируем.
    r = depersonalize("Игорь К. пришёл", names=["Игорь К."])
    assert r.text == "[ФИО] пришёл"
    r2 = depersonalize("К. пришёл и И. ушёл", names=["Игорь К."])
    assert r2.text == "К. пришёл и И. ушёл"


def test_address_masked():
    for text in (
        "г. Москва, ул. Ленина, д. 5, кв. 10",
        "ул. Строителей 23",
        "проспект Мира, 23, квартира 45",
        "Московская область, г. Химки, ул. Рабочая, д. 2а",
    ):
        r = depersonalize(text)
        assert "[АДРЕС]" in r.text, text
        assert any(f.kind == ADDRESS for f in r.findings), text


def test_no_false_positive_clinical_text():
    text = "по 5 мг дважды в день, д. 5 в таблице, значения 10 и 12 записей"
    r = depersonalize(text)
    assert r.text == text
    assert r.findings == ()


def test_no_named_false_positive_official_titles():
    # Без deny-list «Министерство Здравоохранения РФ» — не ФИО (нет эвристики NER).
    text = "согласно Министерство Здравоохранения РФ"
    r = depersonalize(text)
    assert r.text == text


def test_overlap_email_wins_over_fio_local_part():
    # «иванов@mail.ru» — email целиком, а не ФИО «Иванов» + хвост.
    r = depersonalize("почта иванов@mail.ru", names=["Иванов Иван Иванович"])
    assert r.text == "почта [EMAIL]"
    assert r.findings[0].kind == EMAIL


def test_redact_mode_removes():
    r = depersonalize("тел +7 999 123-45-67, Иванов Иван", names=["Иванов Иван"], mode="redact")
    assert r.text == "тел , "
    assert r.findings


def test_idempotent():
    names = ["Иванов Иван Иванович"]
    first = depersonalize(
        "Иванов Иван Иванович, +7 999 123-45-67, г. Москва, ул. Ленина 5", names=names
    )
    second = depersonalize(first.text, names=names)
    assert second.text == first.text
    assert second.findings == ()


def test_empty_and_no_names():
    assert depersonalize("").text == ""
    assert depersonalize("обычный текст без ПДн").findings == ()


def test_findings_coordinates_match_original():
    original = "звонил +7 999 123-45-67 сегодня"
    r = depersonalize(original)
    f = r.findings[0]
    assert f.original == "+7 999 123-45-67"
    assert original[f.start:f.end] == f.original

# --- структурные формы и валидаторы (docs/12, бенчмарк clinical_contexts) ---

def test_phone_all_real_world_forms():
    for text in ("9261234567", "926 123-45-67", "8-926-123-45-67",
                 "8 (926) 123-45-67", "+79261234567"):
        r = depersonalize(text)
        assert r.text == "[ТЕЛЕФОН]", text
        assert r.findings[0].kind == PHONE


def test_oms_with_spaces_and_continuous():
    for text in ("полис 1234 5678 9012 3456", "1234 5678 9012 3456"):
        r = depersonalize(text)
        assert "[ДОКУМЕНТ]" in r.text, text
        assert r.findings[-1].category == "OMS"


def test_medcard_and_history_number_forms():
    for text, number in (("Медкарта № 456123", "456123"),
                         ("ИБ № 45678/2024", "45678/2024"),
                         ("история болезни 987654", "987654"),
                         ("медкарта № 77-004521", "77-004521")):
        r = depersonalize(text)
        assert r.text.endswith("[ДОКУМЕНТ]"), text
        assert r.findings[-1].category == "MEDCARD"
        assert r.findings[-1].original == number


def test_dob_formats():
    for text in ("дата рождения 12.03.1954",
                 "Д.Р. 12-03-1954",
                 "родился 1985-04-17",
                 "дата рождения 9 августа 1949",
                 "12.03.1954 г.р."):
        r = depersonalize(text)
        assert "[ДАТА РОЖДЕНИЯ]" in r.text, text
        assert r.findings[-1].kind == DOB


def test_structural_validators():
    from compass.depersonalize import inn_valid, normalize_date, normalize_phone, oms_valid, snils_valid

    assert snils_valid("112-233-445 95")  # контрольное число алгоритма ПФР
    assert not snils_valid("123-456-789 01")  # формат верный, контрольное — нет
    assert inn_valid("7707083893")
    assert not inn_valid("1234567890")
    assert inn_valid("500100732259")
    assert oms_valid("1234567890123454")
    assert normalize_phone("8 (926) 123-45-67") == "+79261234567"
    assert normalize_phone("9261234567") == "+79261234567"
    assert normalize_phone("+7 916 555-77-33") == "+79165557733"
    assert normalize_date("9 августа 1949") == "1949-08-09"
    assert normalize_date("12/03/54") == "1954-03-12"
    assert normalize_date("1985-04-17") == "1985-04-17"


def test_span_level_masking_handles_overlaps():
    from compass.depersonalize import Span

    text = "Иванов Пётр Сергеевич, +79261234567"
    # частичный и полный спан ФИО + телефон от NER — один плейсхолдер на сущность
    r = depersonalize(text, extra_spans=[
        Span("FIO", 0, 6),
        Span("FIO", 0, len("Иванов Пётр Сергеевич")),
        Span("PHONE", 24, 36),
    ])
    assert r.text == "[ФИО], [ТЕЛЕФОН]"
    assert len(r.findings) == 2


def test_age_combo_requires_fio():
    from compass.depersonalize import Span

    text = "Пациентка Петрова Анна, 64 года"
    start = text.index("Петрова")
    masked = depersonalize(text, extra_spans=[Span("FIO", start, start + len("Петрова Анна"))])
    assert "[ВОЗРАСТ]" in masked.text
    plain = depersonalize(text)
    assert plain.text == text
