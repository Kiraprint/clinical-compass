"""Деперсонализация текста перед отправкой на LLM API (docs/12).

Чистый модуль без I/O и без внешних зависимостей (только stdlib re) — по
контракту ядра (docs/03 §1.3): офлайн, детерминированно, тестируемо.

Что маскируется:
- ФИО — deny-list известных имён (пациенты из ростра: система знает, кто её
  пациенты) + варианты с инициалами; без NER-эвристик (ложные срабатывания на
  официальные названия: «Министерство Здравоохранения РФ»);
- адреса — улица/город/дом/квартира по опорным словам (ул./проспект/г./д. и т.п.);
- номера документов — СНИЛС (3-3-3 2), ИНН (10/12), ОМС (16), паспорт
  (серия 4 + номер 6, с пробелом или слитно);
- телефон РФ (+7/8 …);
- email (в т.ч. кириллический local-part).

Метод обезличивания — замена (приказ Роскомнадзора №996, метод «замена»),
режим redact — сокращение. На выходе placeholders вида [ФИО]/[АДРЕС]/
[ДОКУМЕНТ]/[ТЕЛЕФОН]/[EMAIL] — сохраняют структуру предложения для LLM.

Продуктовый апгрейд до нейросетевого NER (Microsoft Presidio + spaCy,
natasha) — за SPI с той же сигнатурой; в демо не тянем (детерминизм, офлайн,
тяжёлые зависимости, качество RU-моделей).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

# Классы найденного (ключи placeholder-замен).
FIO = "FIO"
ADDRESS = "ADDRESS"
DOC = "DOC"
PHONE = "PHONE"
EMAIL = "EMAIL"

_PLACEHOLDER: dict[str, str] = {
    FIO: "[ФИО]",
    ADDRESS: "[АДРЕС]",
    DOC: "[ДОКУМЕНТ]",
    PHONE: "[ТЕЛЕФОН]",
    EMAIL: "[EMAIL]",
}

# Приоритет при одинаковой позиции старта (меньше = важнее): ФИО важнее
# адреса «ул. Иванова», адрес «д. 5» важнее телефоноподобного «5» и т.п.
# Но при равном старте длиннее совпадение побеждает (email «иванов@mail.ru»
# целиком, а не локальная часть «иванов» как ФИО).
_PRIORITY = {FIO: 0, ADDRESS: 1, DOC: 2, PHONE: 3, EMAIL: 4}

_PHONE = re.compile(
    r"(?:\+7|8)\s?[-\s(]?\d{3}[-\s)]?\s?\d{3}[-\s]?\d{2}[-\s]?\d{2}"
)
# local-part может быть кириллическим (RFC 6531); TLD — буквы/цифры, без
# хвостовой пунктуации на конце предложения.
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-zА-Яа-яЁё0-9-]{2,}")

# Номера документов — жёсткие формы, ложных срабатываний на «по 5 мг» нет.
_SNILS = re.compile(r"\b\d{3}-\d{3}-\d{3}\s\d{2}\b")
_LONG_DIGITS = re.compile(r"\b\d{16}\b|\b\d{12}\b|\b\d{10}\b")
_PASSPORT = re.compile(r"\b\d{4}\s\d{6}\b")

# Адрес. Опорное слово + название (одно/два слова с заглавной) + опциональный
# хвост «, д. 5, кв. 10» / « 5» / «, 23»/«д. 5 кв. 10».
_NUM = r"\d+[а-яё]?"
_UNIT = r"(?:д(?:ом)?\.?|кв\.|квартира|корп\.|корпус|стр\.|строение)"
_NAME_WORD = r"[А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ][а-яё\-]+)?"
_TAIL = rf"(?:\s*,?\s*(?:{_UNIT}\s*)?{_NUM}){{0,3}}"
_STREET_WORDS = r"ул\.|улица|просп\.|проспект|пер\.|переулок|бульв\.|бульвар|наб\.|набережная|шоссе|аллея|площадь|пл\."
_CITY_WORDS = r"г\.|город|пос\.|поселок|посёлок|с\.|село|дер\.|деревня|обл\.|область|край|респ\.|республика|р-н|район"
# Адрес матчится только через опорное слово (ул./г./обл.) — самостоятельный
# «д. 5» не маскируем: в клиническом тексте это скорее «документ/день 5».
_ADDRESS = re.compile(
    rf"(?:{_STREET_WORDS}|{_CITY_WORDS})\s+{_NAME_WORD}{_TAIL}"
)

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (EMAIL, _EMAIL),
    (PHONE, _PHONE),
    (DOC, _SNILS),
    (DOC, _LONG_DIGITS),
    (DOC, _PASSPORT),
    (ADDRESS, _ADDRESS),
]


@dataclass(frozen=True)
class Finding:
    """Найденный фрагмент ПДн в координатах исходного текста."""

    kind: str
    start: int
    end: int
    original: str


@dataclass(frozen=True)
class Depersonalized:
    text: str
    findings: tuple[Finding, ...]


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
        variants.append(f"{esc[0]}\s+{re.escape(initials)}")
        variants.append(f"{esc[0]}\s+{''.join(f'{w[0]}.' for w in esc[1:])}")
        variants.append(" ".join(esc[:2]))
    # Отдельно — основа первого слова в любом падеже (в русском ФИО это
    # фамилия; для «Игорь К.» — имя). [а-яё]{0,3} — короткие окончания.
    stem = _stem(words[0])
    if len(stem) >= 3:
        variants.append(f"{re.escape(stem)}[а-яё]{{0,3}}")
        if len(esc) >= 2:
            initials = " ".join(f"{w[0]}." for w in esc[1:])
            variants.append(f"{re.escape(stem)}[а-яё]{{0,3}}\s+{re.escape(initials)}")
    return [re.compile(v, re.IGNORECASE) for v in variants]


def _collect(text: str, patterns: list[tuple[str, re.Pattern[str]]]) -> list[tuple[int, int, str, str]]:
    found: list[tuple[int, int, str, str]] = []
    for kind, rx in patterns:
        for m in rx.finditer(text):
            found.append((m.start(), m.end(), kind, m.group(0)))
    return found


def depersonalize(
    text: str,
    names: Sequence[str] | None = None,
    *,
    mode: str = "replace",
) -> Depersonalized:
    """Маскирует ПДн в text.

    names — deny-list известных ФИО (например, ФИО пациентов из ростра врача);
    если пусто — маскируются только структурные сущности (телефон/email/документы/
    адреса). mode="replace" — подстановка [КАТЕГОРИЯ], "redact" — удаление.

    Идемпотентно: placeholders не матчатся повторно.
    """
    if not text:
        return Depersonalized(text, ())

    patterns: list[tuple[str, re.Pattern[str]]] = list(_PATTERNS)
    found: list[tuple[int, int, str, str]] = []
    if names:
        for name in names:
            for rx in _fio_patterns(name):
                for m in rx.finditer(text):
                    found.append((m.start(), m.end(), FIO, m.group(0)))
    found += _collect(text, patterns)
    if not found:
        return Depersonalized(text, ())

    # Сортировка: старт; при равном старте — длиннее совпадение; затем приоритет
    # категории (ФИО «ул. Иванова» лежит внутри адреса, но стартует позже).
    found.sort(key=lambda f: (f[0], -len(f[3]), _PRIORITY[f[2]]))

    kept: list[tuple[int, int, str, str]] = []
    last_end = -1
    for f in found:
        if f[0] < last_end:
            continue
        kept.append(f)
        last_end = f[1]

    out: list[str] = []
    pos = 0
    findings: list[Finding] = []
    for start, end, kind, original in kept:
        out.append(text[pos:start])
        out.append("" if mode == "redact" else _PLACEHOLDER[kind])
        findings.append(Finding(kind=kind, start=start, end=end, original=original))
        pos = end
    out.append(text[pos:])

    return Depersonalized("".join(out), tuple(findings))