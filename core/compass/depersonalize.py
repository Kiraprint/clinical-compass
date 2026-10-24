"""Деперсонализация текста перед отправкой на LLM API (docs/12).

Чистый модуль без I/O и без внешних зависимостей (только stdlib re) — по
контракту ядра (docs/03 §1.3): офлайн, детерминированно, тестируемо.

Что маскируется:
- ФИО — deny-list известных имён (ростер врача) + спаны нейро-NER, переданные
  снаружи через ``extra_spans`` (api/app/services/depersonalize.py);
- адреса — улица/город/область/дом/квартира по опорным словам
  (ул./просп./пр-т/пер./наб./г./обл. и т.п.), с расширением влево на город;
- номера документов — СНИЛС (3-3-3 2 и слитно), ИНН (10/12, контрольное
  число), ОМС (16 цифр, контрольная сумма или маркер «полис/ОМС»), паспорт
  (серия 4 + номер 6), медкарта/ИБ (маркер «медкарта/ИБ/история болезни»);
- телефон РФ — +7/8 с любыми разделителями и без, а также 10-значный
  мобильный (9…): ``+79261234567``, ``8 926 123-45-67``, ``9261234567``;
- email (в т.ч. кириллический local-part);
- дата рождения — с явным маркером: «дата рождения», «д.р.», «род.»
  «родился/родилась», суффикс «г.р.»; форматы дд.мм.гггг, дд-мм-гггг,
  гггг-мм-дд, «12 марта 1954». Дата без маркера (приём, начало симптомов)
  не маскируется — это клинические данные, а не ПДн;
- возраст — только в связке с ФИО в том же предложении (комбо-идентификатор);
  отдельно взятый «64 года» уходит в LLM как клинический показатель.

Метод обезличивания — замена (приказ Роскомнадзора №996, метод «замена»),
режим redact — сокращение. На выходе placeholders вида [ФИО]/[АДРЕС]/
[ДОКУМЕНТ]/[ТЕЛЕФОН]/[EMAIL] — сохраняют структуру предложения для LLM.

Маскирование спановое: все кандидаты (структурные + внешние NER) собираются
как (вид, start, end), пересечения разрешаются детерминированно (длиннее и
приоритетнее выигрывает), поэтому частичные/вложенные совпадения не дают
«дырок» в тексте.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

# Классы найденного (ключи placeholder-замен).
FIO = "FIO"
ADDRESS = "ADDRESS"
DOC = "DOC"
DOB = "DOB"
PHONE = "PHONE"
EMAIL = "EMAIL"
AGE = "AGE"

# Точные категории документов в Finding.category (вид остаётся DOC ради
# совместимости плейсхолдеров и отчётов).
SNILS = "SNILS"
INN = "INN"
OMS = "OMS"
PASSPORT = "PASSPORT"
MEDCARD = "MEDCARD"

_PLACEHOLDER: dict[str, str] = {
    FIO: "[ФИО]",
    ADDRESS: "[АДРЕС]",
    DOC: "[ДОКУМЕНТ]",
    DOB: "[ДАТА РОЖДЕНИЯ]",
    PHONE: "[ТЕЛЕФОН]",
    EMAIL: "[EMAIL]",
    AGE: "[ВОЗРАСТ]",
}

# Приоритет при одинаковой позиции старта (меньше = важнее): ФИО важнее
# адреса «ул. Иванова», адрес «д. 5» важнее телефоноподобного «5» и т.п.
# Но при равном старте длиннее совпадение побеждает (email «иванов@mail.ru»
# целиком, а не локальная часть «иванов» как ФИО).
_PRIORITY = {FIO: 0, ADDRESS: 1, DOB: 2, DOC: 3, AGE: 4, PHONE: 5, EMAIL: 6}


@dataclass(frozen=True)
class Finding:
    """Найденный фрагмент ПДн в координатах исходного текста."""

    kind: str
    start: int
    end: int
    original: str
    category: str | None = None  # SNILS/INN/OMS/PASSPORT/MEDCARD для DOC


@dataclass(frozen=True)
class Depersonalized:
    text: str
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class Span:
    """Кандидат-сущность до разрешения пересечений (в т.ч. внешний NER)."""

    kind: str
    start: int
    end: int
    category: str | None = None


# ------------------------------------------------------------------ валидаторы

def snils_checksum(digits: str) -> int:
    """Контрольное число СНИЛС по первым 9 цифрам (алгоритм ПФР)."""
    nums = [int(c) for c in digits[:9]]
    total = sum(d * (9 - i) for i, d in enumerate(nums))
    if total < 100:
        return total
    if total in (100, 101):
        return 0
    ctrl = total % 101
    return 0 if ctrl == 100 else ctrl


def snils_valid(value: str) -> bool:
    """True, если 11 цифр (с разделителями или без) — корректный СНИЛС."""
    digits = re.sub(r"\D", "", value)
    if len(digits) != 11:
        return False
    return snils_checksum(digits) == int(digits[9:])


def inn_valid(value: str) -> bool:
    """Контрольные числа ИНН: 10 цифр (ЮЛ) или 12 (ФЛ/ИП)."""
    digits = re.sub(r"\D", "", value)
    if len(digits) not in (10, 12):
        return False
    d = [int(c) for c in digits]
    w10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    if len(d) == 10:
        return sum(x * y for x, y in zip(d[:9], w10)) % 11 % 10 == d[9]
    w11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    w12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    c11 = sum(x * y for x, y in zip(d[:10], w11)) % 11 % 10
    c12 = sum(x * y for x, y in zip(d[:11], w12)) % 11 % 10
    return c11 == d[10] and c12 == d[11]


def oms_valid(value: str) -> bool:
    """Правдоподобие полиса ОМС (16 цифр): контрольная цифра = сумма нечётных.

    Полный алгоритм ФФОМС сложнее; здесь проверка отсекает случайные 16-значные
    числа (номера карт), при этом полис с корректной формой маскируется ещё и
    по маркеру «полис/ОМС» (см. structural_spans).
    """
    digits = re.sub(r"\D", "", value)
    if len(digits) != 16:
        return False
    return sum(int(c) for c in digits[0::2]) % 10 == int(digits[15])


_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
}

_DATE_RE = re.compile(
    r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}"      # 12.03.1954, 12-03-54, 12/03/1954
    r"|\d{4}-\d{2}-\d{2}"                     # 1954-03-12
    r"|\d{1,2}\s+(?:"
    + "|".join(_MONTHS)
    + r")\s+\d{4}(?:\s*г\.?)?"                # 12 марта 1954
)


def normalize_date(value: str) -> str | None:
    """Приводит дату к ISO (YYYY-MM-DD); None — если формат не распознан."""
    s = value.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", s)
    if m:
        day, month, year = (int(x) for x in m.groups())
        if year < 100:
            year += 1900 if year > 30 else 2000
        return f"{year:04d}-{month:02d}-{day:02d}"
    m = re.fullmatch(
        r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})(?:\s*г\.?)?", s
    )
    if m:
        day, month, year = int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def normalize_phone(value: str) -> str | None:
    """E.164-канон российского номера (+7XXXXXXXXXX) или None."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    return None


# ------------------------------------------------------------------ структура

# Email: local-part может быть кириллическим (RFC 6531); TLD — буквы/цифры,
# без хвостовой пунктуации на конце предложения.
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-zА-Яа-яЁё0-9-]{2,}")

# Телефон: +7/8 с разделителями или 10-значный мобильный 9XXXXXXXXX.
_PHONE = re.compile(
    r"(?<![\d+])(?:"
    r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
    r"|9\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
    r")(?!\d)"
)

_SNILS_HYPHEN = re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}\s\d{2}(?!\d)")
_DIGITS_RUN = re.compile(r"(?<![\d+])\d{10,16}(?!\d)")
# Паспорт: серия 4 + номер 6 — только с пробелом/№ или явным маркером,
# иначе слитные 10 цифр (телефон/ИНН) ложно считались бы паспортом.
_PASSPORT = re.compile(r"(?<!\d)\d{4}\s(?:№\s?)?\d{6}(?!\d)")
_PASSPORT_MARKED = re.compile(
    r"паспорт\w*\s*(?:серия\s*)?№?\s*(\d{4}\s?№?\s?\d{6})(?!\d)", re.IGNORECASE
)
_OMS_SPACED = re.compile(r"(?<!\d)\d{4}\s\d{4}\s\d{4}\s\d{4}(?!\d)")

_MEDCARD = re.compile(
    r"(?:(?:мед(?:ицинская)?\.?\s*)?карт[аыу]|история\s+болезни|ИБ)\s*"
    r"№?\s*(\d{2,7}(?:[-/]\d{2,7}){0,2})",
    re.IGNORECASE,
)

# Дата рождения — только с явным маркером (см. docstring модуля).
_DOB_PREFIX = re.compile(
    r"(?:дата\s+рождения|д\.\s?р\.?|род\.|родил(?:ся|ась))\s*:?\s*"
    r"(?P<date>" + _DATE_RE.pattern + r")",
    re.IGNORECASE,
)
_DOB_SUFFIX = re.compile(
    r"(?P<date>" + _DATE_RE.pattern + r")\s*(?:г\.\s?р\.|года\s+рождения)",
    re.IGNORECASE,
)

_AGE = re.compile(r"(?<![\d])\d{1,3}\s*(?:лет|года|г\.)(?![\w])")

# Адрес. Опорное слово + название (одно/два слова с заглавной) + хвост
# «, д. 5, кв. 10» / « 5» / «д. 5 кв. 10».
_NUM = r"\d+[а-яёА-Я]?"
_UNIT = r"(?:д(?:ом)?|кв(?:артира)?|корп(?:ус)?|стр(?:оение)?|лит(?:ера)?)"
_TAIL = rf"(?:[,\s]+(?:{_UNIT}\.?\s*)?{_NUM})*"
_STREET = (
    r"(?:ул\.|улица|просп\.|проспект|пр-т|пр-кт|пер\.|переулок|"
    r"бульв\.|бульвар|наб\.|набережная|шоссе|аллея|площадь|пл\.|"
    r"мкр\.|микрорайон)"
)
_NAME = r"[А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ][а-яё\-]+)?"
_TAIL_ONE = rf"[,\s]+(?:{_UNIT}\.?\s*)?{_NUM}"
_ADDRESS = re.compile(rf"{_STREET}\s+{_NAME}{_TAIL}")
# «Лиговский пр-т, д. 55» — название улицы перед опорным словом. Обязателен
# номер дома: без него «Добрый день, пер.» давало бы ложные адреса.
_ADDRESS_NAME_FIRST = re.compile(
    rf"{_NAME}\s+{_STREET}(?:{_TAIL_ONE}){{1,3}}"
)

# Сегмент слева от улицы, который тоже относится к адресу: «г. Москва,»,
# «Московская обл.,», «Санкт-Петербург,» (город через дефис).
_ADDR_LEFT = re.compile(
    r"(?:(?P<label>г\.|город|пос\.|пос[её]лок|с\.|село|дер\.|деревня|пгт)\s+"
    r"(?P<name>[А-ЯЁ][а-яё\-]+(?:-[А-ЯЁ][а-яё\-]+)?)"
    r"|(?P<name2>[А-ЯЁ][а-яё\-]+(?:-[А-ЯЁ][а-яё\-]+)?)\s+"
    r"(?P<region>обл\.|область|край|р-н|район|респ\.|республика)"
    r"|(?P<city>[А-ЯЁ][а-яё]+-[А-ЯЁ][а-яё\-]+))"
    r"\s*,\s*$"
)


def _add(spans: list[Span], kind: str, start: int, end: int,
         category: str | None = None) -> None:
    if end > start:
        spans.append(Span(kind, start, end, category))


def _marker_before(text: str, start: int, words: str, window: int = 18) -> bool:
    return re.search(rf"(?:{words})", text[max(0, start - window):start],
                     re.IGNORECASE) is not None


def _phone_spans(text: str) -> list[Span]:
    out: list[Span] = []
    for m in _PHONE.finditer(text):
        # «ИНН 9261234567»/«СНИЛС …» — цифры при документном маркере, не номер.
        if _marker_before(text, m.start(), r"ИНН|СНИЛС|паспорт|полис|медкарт"):
            continue
        if normalize_phone(m.group(0)):
            _add(out, PHONE, m.start(), m.end())
    return out


def _doc_spans(text: str) -> list[Span]:
    """СНИЛС/ИНН/ОМС/паспорт/медкарта с валидацией и маркерами."""
    out: list[Span] = []
    claimed: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in claimed)

    # Формат с разделителями однозначен: маскируем даже при битой контрольной
    # сумме (опечатка в карте не должна превращаться в утечку).
    for m in _SNILS_HYPHEN.finditer(text):
        if free(m.start(), m.end()):
            _add(out, DOC, m.start(), m.end(), SNILS)
            claimed.append((m.start(), m.end()))

    for m in _OMS_SPACED.finditer(text):
        if free(m.start(), m.end()):
            _add(out, DOC, m.start(), m.end(), OMS)
            claimed.append((m.start(), m.end()))

    for m in _DIGITS_RUN.finditer(text):
        val = m.group(0)
        a, b = m.start(), m.end()
        if not free(a, b):
            continue
        if len(val) == 16:
            if oms_valid(val) or _marker_before(text, a, r"полис|ОМС"):
                _add(out, DOC, a, b, OMS)
                claimed.append((a, b))
        elif len(val) == 12:
            if inn_valid(val) or _marker_before(text, a, r"ИНН"):
                _add(out, DOC, a, b, INN)
                claimed.append((a, b))
        elif len(val) == 11:
            # 11 цифр: телефон 7/8XXXXXXXXXX, СНИЛС без разделителей или
            marker = _marker_before(text, a, r"СНИЛС")
            if marker:
                _add(out, DOC, a, b, SNILS)
                claimed.append((a, b))
            elif snils_valid(val) and val[0] not in "78":
                _add(out, DOC, a, b, SNILS)
                claimed.append((a, b))
            elif val[0] in "78" and normalize_phone(val):
                _add(out, PHONE, a, b)
                claimed.append((a, b))
            elif snils_valid(val):
                _add(out, DOC, a, b, SNILS)
                claimed.append((a, b))
        elif len(val) == 10:
            if _marker_before(text, a, r"паспорт"):
                # «паспорт: 4510123456» — серия и номер без пробела: маскируем
                # по маркеру, не полагаясь на контрольную сумму ИНН.
                _add(out, DOC, a, b, PASSPORT)
                claimed.append((a, b))
            elif val[0] == "9" and not _marker_before(text, a, r"ИНН"):
                _add(out, PHONE, a, b)
                claimed.append((a, b))
            elif inn_valid(val) or _marker_before(text, a, r"ИНН"):
                _add(out, DOC, a, b, INN)
                claimed.append((a, b))

    for m in _PASSPORT.finditer(text):
        if free(m.start(), m.end()) and not m.group(0).startswith("0"):
            _add(out, DOC, m.start(), m.end(), PASSPORT)
            claimed.append((m.start(), m.end()))

    for m in _PASSPORT_MARKED.finditer(text):
        a, b = m.span(1)
        if free(a, b):
            _add(out, DOC, a, b, PASSPORT)
            claimed.append((a, b))

    for m in _MEDCARD.finditer(text):
        a, b = m.span(1)
        if free(a, b):
            _add(out, DOC, a, b, MEDCARD)
            claimed.append((a, b))
    return out


def _dob_spans(text: str) -> list[Span]:
    out: list[Span] = []
    seen: set[tuple[int, int]] = set()
    for rx in (_DOB_PREFIX, _DOB_SUFFIX):
        for m in rx.finditer(text):
            a, b = m.span("date")
            if (a, b) in seen:
                continue
            if normalize_date(m.group("date")) is None:
                continue
            seen.add((a, b))
            _add(out, DOB, a, b)
    return out


def _address_spans(text: str) -> list[Span]:
    out: list[Span] = []
    for rx in (_ADDRESS, _ADDRESS_NAME_FIRST):
        for m in rx.finditer(text):
            start, end = m.start(), m.end()
            # расширяем влево на город/область: «г. Москва, ул. Ленина…»
            while True:
                left = _ADDR_LEFT.search(text, max(0, start - 60), start)
                if not left or left.end() != start:
                    break
                start = left.start()
            _add(out, ADDRESS, start, end)
    return out


def structural_spans(text: str) -> list[Span]:
    """Все структурные ПДн-спаны без deny-list и без внешнего NER."""
    spans: list[Span] = []
    spans += [Span(EMAIL, m.start(), m.end()) for m in _EMAIL.finditer(text)]
    spans += _phone_spans(text)
    spans += _doc_spans(text)
    spans += _dob_spans(text)
    spans += _address_spans(text)
    spans += [Span(AGE, m.start(), m.end()) for m in _AGE.finditer(text)]
    return spans


# ------------------------------------------------------------------ deny-list

def _stem(word: str) -> str:
    """Грубая основа для русской флексии: «Петрова»→«Петров», «Игорь»→«Игор».

    Достаточно для deny-list (фамилия — главный идентификатор): покрывает
    падежные формы «Петров/Петрова/Петровой/Петровым» при минимуме ложных
    срабатываний.
    """
    return word[:-1] if word[-1:] in ("а", "я", "ь") else word


def _fio_patterns(name: str) -> list[re.Pattern[str]]:
    """Варианты написания одного ФИО: полное, с инициалами, фамилия-основа.

    «Иванов Иван Иванович» → «Иванов Иван Иванович», «Иванов И. И.»,
    «Иванов И.И.», «Иванов Иван», «Иванов/Иванова/Ивановой/…» (флексия).
    Фамилия длиной <3 (напр. «К.») не маскируется отдельно — слишком много
    ложных срабатываний.
    """
    words = name.split()
    if not words:
        return []
    esc = [re.escape(w) for w in words]
    variants = [" ".join(esc)]
    if len(esc) >= 2:
        initials = " ".join(f"{w[0]}." for w in esc[1:])
        variants.append(rf"{esc[0]}\s+{re.escape(initials)}")
        variants.append(rf"{esc[0]}\s+{''.join(f'{w[0]}.' for w in esc[1:])}")
        variants.append(" ".join(esc[:2]))
    # Отдельно — основа первого слова в любом падеже (в русском ФИО это
    # фамилия; для «Игорь К.» — имя). [а-яё]{0,3} — короткие окончания.
    stem = _stem(words[0])
    if len(stem) >= 3:
        variants.append(f"{re.escape(stem)}[а-яё]{{0,3}}")
        if len(esc) >= 2:
            initials = " ".join(f"{w[0]}." for w in esc[1:])
            variants.append(rf"{re.escape(stem)}[а-яё]{{0,3}}\s+{re.escape(initials)}")
    return [re.compile(v, re.IGNORECASE) for v in variants]


def _deny_spans(text: str, names: Sequence[str] | None) -> list[Span]:
    out: list[Span] = []
    for name in names or ():
        for rx in _fio_patterns(name):
            for m in rx.finditer(text):
                _add(out, FIO, m.start(), m.end())
    return out

def is_plausible_person_span(text: str, span: Span) -> bool:
    """Отсев мусорных NER-«ФИО»: одиночное слово со строчной буквы — не имя.

    rubert на клинических текстах иногда метит «амлодипина»/«аторвастатина»
    как PER. Многословные ФИО (в т.ч. целиком строчные «сидорова галина»)
    оставляем: маскирование лишнего безопаснее пропуска.
    """
    if span.kind != FIO:
        return True
    value = text[span.start:span.end]
    if len(value.split()) > 1:
        return True
    return value[:1].isupper()


# ------------------------------------------------------------------ разрешение

def merge_adjacent_spans(spans: list[Span], text: str) -> list[Span]:
    """Склеивает соседние спаны одного вида через пробел/запятую.

    Нейро-NER часто режет «Иванов Пётр» + «Сергеевич» на два спана одного ФИО.
    """
    ordered = sorted(spans, key=lambda s: (s.start, s.end))
    merged: list[Span] = []
    for s in ordered:
        if merged:
            prev = merged[-1]
            gap = text[prev.end:s.start]
            if (prev.kind == s.kind and prev.category == s.category
                    and gap in ("", " ", ", ", ",") and
                    len(gap) <= 2):
                merged[-1] = Span(prev.kind, prev.start, s.end, prev.category)
                continue
        merged.append(s)
    return merged


def _fio_near(text: str, age_start: int, fio_spans: list[Span],
              window: int = 60) -> bool:
    """ФИО в той же фразе рядом с возрастом (комбо-идентификатор).

    Границу фразы ищем по «[.!?] + пробел», а не по любой точке: инициалы
    «Соколов П.Д., 58 лет» не должны разрывать связку ФИО+возраст.
    """
    for x in fio_spans:
        if abs(x.start - age_start) > window:
            continue
        between = text[min(x.start, age_start):max(x.start, age_start)]
        if re.search(r"[.!?]\s", between) or "\n" in between:
            continue
        return True
    return False




def resolve_spans(spans: Iterable[Span], text: str) -> list[Span]:
    """Разрешает пересечения: длиннее/приоритетнее выигрывает, дыр нет.

    Возраст оставляется только рядом с ФИО в том же предложении (комбо-
    идентификатор); одиночный возраст — клинические данные, не ПДн.
    """
    merged = merge_adjacent_spans(list(spans), text)
    fio_spans = [s for s in merged if s.kind == FIO]
    age_ok: list[Span] = []
    for s in merged:
        if s.kind != AGE:
            continue
        if _fio_near(text, s.start, fio_spans):
            age_ok.append(s)
    candidates = [s for s in merged if s.kind != AGE] + age_ok

    # Дедуп по (start, end), затем сортировка: старт, длиннее, приоритет.
    uniq = {(s.start, s.end, s.kind, s.category): s for s in candidates}
    ordered = sorted(
        uniq.values(),
        key=lambda s: (s.start, -(s.end - s.start), _PRIORITY.get(s.kind, 99)),
    )
    kept: list[Span] = []
    last_end = -1
    for s in ordered:
        if s.start < last_end:
            if s.end > last_end:
                # Частичное перекрытие: хвост ещё не замаскирован. Режем начало,
                # а не выбрасываем спан целиком — иначе третье слово ФИО или
                # хвост телефона уехали бы в LLM открытым текстом.
                kept.append(Span(s.kind, last_end, s.end, s.category))
                last_end = s.end
            continue
        kept.append(s)
        last_end = s.end
    return kept


def mask_spans(text: str, spans: Sequence[Span], *, mode: str = "replace") -> Depersonalized:
    """Подставляет placeholders по готовым (уже непересекающимся) спанам."""
    out: list[str] = []
    findings: list[Finding] = []
    pos = 0
    for s in spans:
        out.append(text[pos:s.start])
        out.append("" if mode == "redact" else _PLACEHOLDER.get(s.kind, "[СКРЫТО]"))
        findings.append(Finding(s.kind, s.start, s.end, text[s.start:s.end],
                                s.category))
        pos = s.end
    out.append(text[pos:])
    return Depersonalized("".join(out), tuple(findings))


def depersonalize(
    text: str,
    names: Sequence[str] | None = None,
    *,
    mode: str = "replace",
    extra_spans: Iterable[Span | tuple[str, int, int]] = (),
) -> Depersonalized:
    """Маскирует ПДн в text.

    names — deny-list известных ФИО (например, ФИО пациентов из ростра врача);
    если пусто — маскируются только структурные сущности (телефон/email/документы/
    адреса). extra_spans — внешняя NER-детекция (вид, start, end) или Span;
    объединяется со структурными спанами и deny-list.

    mode="replace" — подстановка [КАТЕГОРИЯ], "redact" — удаление.
    Идемпотентно: placeholders не матчатся повторно.
    """
    if not text:
        return Depersonalized(text, ())

    spans: list[Span] = structural_spans(text)
    spans += _deny_spans(text, names)
    for item in extra_spans:
        if isinstance(item, Span):
            spans.append(item)
        else:
            kind, start, end = item
            spans.append(Span(str(kind), int(start), int(end)))
    spans = [s for s in spans if 0 <= s.start < s.end <= len(text)]

    if not spans:
        return Depersonalized(text, ())
    return mask_spans(text, resolve_spans(spans, text), mode=mode)


def extract_spans(
    text: str,
    names: Sequence[str] | None = None,
    *,
    extra_spans: Iterable[Span | tuple[str, int, int]] = (),
) -> list[Span]:
    """Публичный доступ к объединённым спанам (бенчмарк, тесты, отладка)."""
    if not text:
        return []
    spans: list[Span] = structural_spans(text) + _deny_spans(text, names)
    for item in extra_spans:
        if isinstance(item, Span):
            spans.append(item)
        else:
            kind, start, end = item
            spans.append(Span(str(kind), int(start), int(end)))
    spans = [s for s in spans if 0 <= s.start < s.end <= len(text)]
    return resolve_spans(spans, text)
