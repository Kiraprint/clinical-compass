"""Тесты деперсонализации (docs/12). Чистая функция из ядра, без I/O."""

import pytest

from compass.depersonalize import (
    ADDRESS,
    DOB,
    DOC,
    EMAIL,
    FIO,
    PHONE,
    Span,
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


def test_partial_overlap_keeps_tail_masked():
    """Пересекающиеся спаны: хвост второго не должен остаться открытым.

    Регресс ревью PR #40: раньше второй спан выбрасывался целиком, и «Иванович»
    после уже замаскированного «Иванов Пётр» уезжал в LLM.
    """
    from compass.depersonalize import Span, mask_spans, resolve_spans

    text = "Иванов Пётр Иванович"
    spans = [Span("FIO", 0, 11), Span("FIO", 7, 21)]
    masked = mask_spans(text, resolve_spans(spans, text)).text
    assert "Иванович" not in masked
    # Пересекающиеся спаны одного вида склеиваются в объединение,
    # поэтому плейсхолдер один и покрывает всё ФИО.
    assert masked.count("[ФИО]") == 1


def test_unknown_span_kind_gets_generic_placeholder():
    """Неизвестный вид (например, новая категория сайдкара) не должен падать."""
    from compass.depersonalize import Span, mask_spans

    out = mask_spans("секрет", [Span("SOMETHING_NEW", 0, 6)]).text
    assert out == "[СКРЫТО]"


# --------------------------------------------- валидаторы и структурные спаны

def test_snils_checksum_branches():
    from compass.depersonalize import snils_checksum, snils_valid

    assert snils_checksum("112233445") < 100          # сумма < 100
    assert snils_checksum("999999999") in (0, 1, 2)   # 100/101 → 0, иначе %101
    assert snils_valid("112-233-445 95")
    assert not snils_valid("112-233-445 94")
    assert not snils_valid("112-233-44")


def test_inn_checksum_10_and_12_digits():
    from compass.depersonalize import inn_valid

    assert inn_valid("7707083893")            # ЮЛ, верная контрольная
    assert not inn_valid("7707083894")
    assert not inn_valid("123456")            # длина не 10/12
    assert inn_valid("500100732259")          # ФЛ, верные две контрольные
    assert not inn_valid("500100732258")


def test_oms_plausibility():
    from compass.depersonalize import oms_valid

    base = "123456789012345"
    good = base + str(sum(int(c) for c in base[0::2]) % 10)
    assert oms_valid(good)
    assert not oms_valid(good[:-1] + str((int(good[-1]) + 1) % 10))
    assert not oms_valid("12345")


def test_digit_runs_with_and_without_markers():
    from compass.depersonalize import depersonalize

    assert "1234567890123456" not in depersonalize("полис 1234567890123456").text
    # 16 цифр без валидной контрольной и без маркера — не полис, остаётся
    plain = "карта лояльности 1234567890123456"
    assert depersonalize(plain).text == plain
    assert "7707083893" not in depersonalize("ИНН 7707083893 плательщика").text
    # 11 цифр: «8XXXXXXXXXX» — телефон, «СНИЛС …» — документ
    assert "89261234567" not in depersonalize("звоните 89261234567").text
    assert "11223344595" not in depersonalize("СНИЛС 11223344595").text


def test_passport_and_medcard_forms():
    from compass.depersonalize import depersonalize

    assert "4510 123456" not in depersonalize("паспорт 4510 123456 выдан").text
    assert "4510123456" not in depersonalize("паспорт: 4510123456").text
    assert "456123" not in depersonalize("медкарта № 456123").text
    assert "12345" not in depersonalize("ИБ 12345 открыта").text


def test_dob_month_name_and_iso_forms():
    from compass.depersonalize import depersonalize

    assert "12 марта 1954" not in depersonalize("д.р. 12 марта 1954 года").text
    assert "1954-03-12" not in depersonalize("дата рождения 1954-03-12").text
    # Несуществующая дата с маркером всё равно маскируется: лучше скрыть лишнее,
    # чем пропустить ДР из-за опечатки (docs/12).
    assert "31.02.1954" not in depersonalize("д.р. 31.02.1954").text


def test_extract_spans_public_api_and_tuple_extras():
    from compass.depersonalize import Span, extract_spans

    text = "Иванов Пётр, +79261234567, 64 года"
    spans = extract_spans(text, ["Иванов Пётр"], extra_spans=[("ADDRESS", 30, 36)])
    kinds = [s.kind for s in spans]
    assert "FIO" in kinds and "PHONE" in kinds
    # невалидный кортеж (вне текста) отфильтрован
    assert all(0 <= s.start < s.end <= len(text) for s in spans)
    assert extract_spans("", None) == []
    assert extract_spans("текст", None, extra_spans=[Span("FIO", 0, 5)])[0].kind == "FIO"


def test_single_word_lowercase_is_not_person():
    from compass.depersonalize import Span, is_plausible_person_span

    text = "принимает амлодипин"
    assert not is_plausible_person_span(text, Span("FIO", 10, 20))
    assert is_plausible_person_span("Иванов", Span("FIO", 0, 6))
    assert is_plausible_person_span("сидорова галина", Span("FIO", 0, 15))

def test_merge_adjacent_spans_is_public_and_joins_split_fio():
    """Нейро-NER режет ФИО на два спана — публичный хелпер их склеивает."""
    from compass.depersonalize import merge_adjacent_spans

    text = "Меня зовут Иванов Пётр Сергеевич"
    merged = merge_adjacent_spans(
        [Span("FIO", 11, 22), Span("FIO", 22, 32)], text
    )
    assert merged == [Span("FIO", 11, 32)]
