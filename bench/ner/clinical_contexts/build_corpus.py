"""Сборка клинического gold-корпуса для деперсонализации (docs/12).

Зачем свой корпус: публичный pii_benchmark (bench/ner/data) размечен на
новостных/произвольных предложениях и содержит 22 категории, часть которых к
нашему egress-пути не относится (деньги, события, URL). Метрика на нём не
отвечает на вопрос «что утечёт в LLM из чата пациента / дневника / вопроса
врача / батча инсайтов».

Корпус — 200+ синтетических контекстов четырёх форм egress-пути:
  patient_chat       — сообщение пациента в чате с врачом
  diary              — свободный текст дневника/чек-ина
  clinician_question — вопрос врача ассистенту
  insights_batch     — батч реплик для chat_insights (FR-31)

Шаблоны и пулы значений написаны вручную (людей нет — все ФИО/документы
вымышлены и генерируются детерминированно). Gold-спаны вычисляются по
конструкции: текст собирается из литералов и подстановок, поэтому границы
сущностей точные, без ручной разметки и без участия моделей.

Категории ровно нашего egress-скоупа: FIO, PHONE, EMAIL, ADDRESS, DOB,
SNILS, INN, OMS, PASSPORT, MEDCARD, AGE (возраст в связке с ФИО — комбо-
идентификатор; отдельно взятый возраст — клинические данные и в gold не
попадает, встречается как decoy).

Запуск:  python bench/ner/clinical_contexts/build_corpus.py
Выход:   bench/ner/clinical_contexts/corpus.jsonl (коммитится, синтетика)
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

OUT = Path(__file__).parent / "corpus.jsonl"

# ---------------------------------------------------------------- пулы
SUR_M = ["Иванов", "Петров", "Сидоров", "Кузнецов", "Смирнов", "Волков",
         "Морозов", "Новиков", "Фёдоров", "Соколов", "Никитин", "Орлов",
         "Зайцев", "Павлов", "Семёнов", "Голубев", "Виноградов", "Козлов"]
SUR_F = [s + "а" for s in SUR_M]
NAME_M = ["Иван", "Пётр", "Алексей", "Сергей", "Дмитрий", "Андрей", "Михаил",
          "Николай", "Владимир", "Олег", "Роман", "Кирилл", "Артём", "Денис"]
NAME_F = ["Анна", "Мария", "Ольга", "Елена", "Наталья", "Ирина", "Татьяна",
          "Светлана", "Екатерина", "Юлия", "Алина", "Вера", "Людмила",
          "Галина", "Ксения"]
PAT_M = ["Иванович", "Петрович", "Сергеевич", "Алексеевич", "Дмитриевич",
         "Николаевич", "Андреевич", "Владимирович"]
PAT_F = ["Ивановна", "Петровна", "Сергеевна", "Алексеевна", "Дмитриевна",
         "Николаевна", "Андреевна", "Владимировна"]

STREETS = [
    "ул. Ленина, д. 5, кв. 10",
    "ул. Пушкина, д. 7, корп. 2, кв. 15",
    "пр-т Мира, д. 100, кв. 32",
    "наб. Волги, д. 3, кв. 41",
    "ул. Гагарина, 12, кв. 4",
    "г. Москва, ул. Арбат, д. 21, кв. 8",
    "Московская обл., г. Химки, ул. Мира, 12, кв. 45",
    "ул. Садовая, д. 18, кв. 3",
    "Санкт-Петербург, Лиговский пр-т, д. 55, кв. 17",
    "г. Казань, ул. Баумана, д. 9, кв. 2",
    "Новосибирская обл., г. Бердск, ул. Лесная, д. 4",
    "пер. Тупой, д. 2, кв. 6",
]
PHONES = [
    "+7 926 123-45-67",
    "8 (926) 123-45-67",
    "89261234567",
    "+79261234567",
    "8 926 123 45 67",
    "926 123-45-67",
    "9261234567",
    "8-926-123-45-67",
    "+7 (916) 555-77-33",
    "89165557733",
]
EMAILS = [
    "ivanov.ii@mail.ru",
    "petrova.a@yandex.ru",
    "sidorov1980@gmail.com",
    "kuznetsov.dm+med@clinic.org",
    "volkova_ta@list.ru",
    "иванов@демо.рф",
    "morozov.s@mail.ru",
    "novikova.e@inbox.ru",
]
DOC_MARKS = ["СНИЛС", "СНИЛС:", "снилс", "СНИЛС №", "СНИЛС"]
DOB_FORMS = [
    "12.03.1954",
    "05.11.1968",
    "23.07.1972",
    "14.02.1980",
    "30.09.1961",
    "07.06.1955",
    "1985-04-17",
    "9 августа 1949",
    "21.12.1963",
    "02.01.1958",
]
AGES = ["64", "71", "58", "43", "67", "76", "52", "39"]


def snils(n: int) -> str:
    """Валидный СНИЛС (алгоритм контрольного числа) в формате XXX-XXX-XXX YY."""
    digits = [(n // (10 ** i)) % 10 for i in range(8, -1, -1)]
    s = sum(d * (9 - i) for i, d in enumerate(digits))
    if s < 100:
        c = s
    elif s in (100, 101):
        c = 0
    else:
        c = s % 101
        if c == 100:
            c = 0
    num = "".join(map(str, digits))
    return f"{num[:3]}-{num[3:6]}-{num[6:9]} {c:02d}"


def inn10(n: str) -> str:
    w = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    d = [int(c) for c in n[:9]]
    c = sum(x * y for x, y in zip(d, w)) % 11 % 10
    return n[:9] + str(c)


def inn12(n: str) -> str:
    w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    d = [int(c) for c in n[:10]]
    c1 = sum(x * y for x, y in zip(d, w1)) % 11 % 10
    d2 = d + [c1]
    c2 = sum(x * y for x, y in zip(d2, w2)) % 11 % 10
    return n[:10] + str(c1) + str(c2)


def oms16(base13: str) -> str:
    """16 цифр полиса ОМС: 13 значащих + 2 контрольных по алгоритму ФФОМС.

    Упрощённая проверка: контрольная сумма чётных/нечётных разрядов
    (совпадает с checksum-функцией в core/compass/depersonalize.py).
    """
    digits = [int(c) for c in base13] + [0, 0, 0]
    d = digits[:16]
    s = sum(d[0::2])
    c = s % 10
    return "".join(map(str, d[:15])) + str(c)


def medcards() -> list[str]:
    return ["№ 123456", "ИБ № 45678/2024", "№ 12-345", "история болезни 987654",
            "медкарта № 0034512", "№ 77-004521"]


# ---------------------------------------------------------------- шаблоны
# {slot} -> значение из пула; вид определяется именем слота.
TEMPLATES: list[tuple[str, str]] = [
    # ---------------- patient_chat
    ("patient_chat", "Здравствуйте! Меня зовут {fio}, я записана к вам на четверг. Телефон для связи {phone}."),
    ("patient_chat", "Добрый день. Это {fio_short}. Не могу дозвониться, перезвоните на {phone}, пожалуйста."),
    ("patient_chat", "Здравствуйте, я {fio}. Мой СНИЛС {snils}, прикреплён к поликлинике по адресу {addr}."),
    ("patient_chat", "Это {fio}. Почта {email}, если удобнее писать туда."),
    ("patient_chat", "Здравствуйте! {fio}, дата рождения {dob_marked}. Записалась на приём, но не пришла смс."),
    ("patient_chat", "Пишу с телефона мужа: {phone}. Я {fio}, у меня аллергия на лидокаин."),
    ("patient_chat", "Здравствуйте, {fio} беспокоит. Принимаю {drug} уже три дня, стало хуже."),
    ("patient_chat", "Меня зовут {fio_lower}, я вчера была на приёме. Температура 38,5, что делать?"),
    ("patient_chat", "Добрый вечер. {fio}. Можно перенести приём? Мой номер {phone}."),
    ("patient_chat", "Здравствуйте! Отправляю результаты анализов. Пациент {fio}, {age_combo}."),
    ("patient_chat", "Я {fio}, проживаю {addr}. Районный врач сказал обратиться к вам."),
    ("patient_chat", "Здравствуйте, это {fio_initials}. Подскажите, можно ли принимать {drug} с {drug2}?"),
    ("patient_chat", "Добрый день! {fio}, email {email}, телефон {phone}. Жду ответа по анализам."),
    ("patient_chat", "Меня зовут {fio}. Медкарта {medcard}, посмотрите, пожалуйста, последний приём."),
    ("patient_chat", "Здравствуйте! Паспорт {passport}, полис {oms} — прикрепиться хочу к вам."),
    ("patient_chat", "{fio_lower}, ИНН {inn}. Нужна справка для налогового вычета."),
    ("patient_chat", "Здравствуйте, {fio}. После укола {drug} поднялось давление 160/95, пульс 95."),
    ("patient_chat", "Это мама {fio_child}, ему {age_child} лет. Температура 39 второй день."),
    ("patient_chat", "Здравствуйте, я {fio}, ИБ {medcard_num}. Хочу уточнить дозировку {drug}."),
    ("patient_chat", "Добрый день! Мой муж {fio_m}, у него СНИЛС {snils}, запишите нас вместе."),
    ("patient_chat", "Здравствуйте! {fio}. Адрес доставки справки: {addr}. Спасибо."),
    ("patient_chat", "Здравствуйте, это {fio}. Д.р. {dob}. Сахар 6.8 натощак, это плохо?"),
    ("patient_chat", "{fio}, {age_combo}, принимаю {drug} 5 мг утром. Можно ли увеличить?"),
    ("patient_chat", "Здравствуйте. Пишу по поводу {fio_r}. Его телефон {phone}, он сам не может."),
    ("patient_chat", "Добрый день, я {fio}. Живу {addr}. Ближайшая аптека далеко, как быть с рецептом?"),
    # ---------------- diary
    ("diary", "Утро. {fio}, {age_combo}. АД 128/82, пульс 72. Принял {drug} 5 мг."),
    ("diary", "Дневник: {fio}. Самочувствие удовлетворительное, боль 3/10, сон 6 часов."),
    ("diary", "Запись от 14.05: {fio}, давление 145/90, приняла {drug}. Вечером 130/85."),
    ("diary", "Сегодня плохо спал. {fio_r}. Головная боль с утра, {drug} не помог. АД 150/95."),
    ("diary", "{fio_m}, {age_combo_short}: кашель усилился ночью, температура 37,8, сатурация 96%."),
    ("diary", "Пропустил приём {drug} вечером. {fio}. Пульс 88, слабость. Позвонить врачу?"),
    ("diary", "Дневник самоконтроля. Пациент {fio}. Сахар: 7.1 / 6.4 / 5.9. {drug} 1000 мг дважды."),
    ("diary", "{fio}, {age_combo}. Тревога 7/10 по шкале, сон 4 часа, аппетит снижен."),
    ("diary", "После прогулки одышка. {fio}. Прошёл 300 м, остановился. {drug} ингаляция 1 доза."),
    ("diary", "Чек-ин: настроение 4/10, энергия 2/10. {fio_r}, {age_combo}."),
    ("diary", "Записал: {fio}, вес 82 кг, отёков нет, {drug} переносит нормально."),
    ("diary", "Здравствуйте, доктор. {fio}. Ночью был приступ, вызвали скорую, {addr}."),
    ("diary", "Дневник. {fio_m}, {age_combo}. Боль в груди 2/10, нитроглицерин не потребовался."),
    ("diary", "Сегодня {date_clin} — плановый приём. {fio}. Подготовился, не ел с вечера."),
    ("diary", "Показатели: АД 118/76, ЧСС 68, SpO2 98%. {fio}, самочувствие хорошее."),
    ("diary", "{fio}. Дневник за неделю: сон 5-7 ч, тревога 5/10, {drug} по схеме."),
    # ---------------- clinician_question
    ("clinician_question", "Пациент {fio}, {age_combo}, СНИЛС {snils}. Как корректировать {drug} при СКФ 45?"),
    ("clinician_question", "Женщина {age_age}, {fio}, ИБ {medcard_num}. Жалобы на сердцебиение. Тактика?"),
    ("clinician_question", "Коллеги, {fio}, {age_combo}. На {drug} развилась крапивница. Чем заменить?"),
    ("clinician_question", "Пациент {fio_m}, {age_combo_short}, поступил с АД 190/110. Схема снижения?"),
    ("clinician_question", "{fio}, {age_combo}. Хочу назначить {drug}, но ХБП 3а. Дозы?"),
    ("clinician_question", "Пациентка {fio_f}, {age_combo}. Беременность 12 недель, можно ли {drug}?"),
    ("clinician_question", "Что делать с {fio} ({age_combo}) при тромбоцитах 90? {drug} отменить?"),
    ("clinician_question", "Коллега, {fio}. Дата рождения {dob_marked}. Подозрение на СД 2 типа, какие анализы?"),
    ("clinician_question", "У {fio_r} на ЭКГ блокада левой ножки. Жалобы типичные. {drug} можно?"),
    ("clinician_question", "Пациент {fio}, {age_combo}, аллергия на пенициллин. Чем прикрыть {drug}?"),
    ("clinician_question", "Коллеги, {fio_m} ({age_combo_short}) с ХОБЛ, сатурация 91%. Нужна ли госпитализация?"),
    ("clinician_question", "{fio}. Дневник показывает эпизоды гипогликемии до 3.4. {drug} снижаем?"),
    ("clinician_question", "Проверьте, пожалуйста: {fio}, {age_combo}, принимает {drug} и {drug2}. Совместимость?"),
    ("clinician_question", "У пациентки {fio_f} ({age_combo}) повышен ТТГ 8.2. Когда начинать {drug}?"),
    ("clinician_question", "Коллеги, {fio}. Индекс массы тела 34, АД 150/95. С чего начать?"),
    ("clinician_question", "Пациент {fio}, ИБ {medcard_num}. Выписан вчера, снова боль. Что исключить?"),
    # ---------------- insights_batch
    ("insights_batch", "Пациент: {fio}. Пишет: 'здравствуйте, температура 38'. Ответ врача: 'принимайте {drug}'. Пациент: 'спасибо, {phone} для связи'."),
    ("insights_batch", "Пациент: {fio_r}. Сообщение: 'не могу записаться, телефон {phone}'. Сообщение: 'дозвонилась, спасибо'."),
    ("insights_batch", "{fio}. 'АД 160/100, выпил {drug}'. 'Стало 140/90'. 'Завтра на приём к вам'."),
    ("insights_batch", "Диалог с {fio}: 'можно ли {drug} с {drug2}?' 'нельзя, разнесите по времени'. 'понял, спасибо'."),
    ("insights_batch", "Пациентка {fio_f}, {age_combo}. 'Сон 4 часа, тревога 8'. 'Держится давление'. 'Пью {drug}'."),
    ("insights_batch", "Пациент {fio_m}. 'Забыл, как принимать {drug}'. 'Дозировка 5 мг утром'. 'Спасибо, {email} мой'."),
    ("insights_batch", "Переписка. 'Здравствуйте, {fio}'. 'Да, слушаю'. 'СНИЛС {snils} продиктовать?' 'Да, спасибо'."),
    ("insights_batch", "'Добрый день, {fio_initials}, {age_age}'. 'Беспокоит кашель'. 'С {date_clin} хуже'. 'Пью {drug}'."),
    ("insights_batch", "Пациент: 'это {fio}, я потеряла полис {oms}'. Врач: 'восстановите в поликлинике'. Пациент: 'поняла'."),
    ("insights_batch", "'Меня зовут {fio}'. 'Хочу перенести приём'. 'Адрес {addr} верный?' 'Да, всё верно'."),
    ("insights_batch", "Пациент {fio}. 'Сахар 9.2 утром'. 'Почему так?' 'Вечером был торт'. Врач: 'вернём {drug} 1000 мг'."),
    ("insights_batch", "'Это {fio_short}, д.р. {dob}'. 'Поняла'. 'Можно {drug}?' 'Можно, по схеме'."),
    ("insights_batch", "Пациент: {fio}. 'Нужна справка, паспорт {passport}'. Врач: 'приходите завтра'. Пациент: 'спасибо'."),
    # ---------------- документы и даты рождения (расширение баланса категорий)
    ("patient_chat", "Документы для прикрепления: паспорт {passport}, полис ОМС {oms}, ИНН {inn}, СНИЛС {snils}."),
    ("patient_chat", "Здравствуйте, полис {oms} не читается в регистратуре, что делать? {fio}"),
    ("patient_chat", "Нужна справка для вычета, ИНН {inn}, телефон {phone}."),
    ("patient_chat", "Здравствуйте! {fio}, родился {dob}. Можно ли прийти без записи?"),
    ("patient_chat", "Это {fio}, {dob_gr} г.р. Раньше наблюдалась у вас, карту потеряла."),
    ("patient_chat", "{fio}, дата рождения {dob_word}. Беспокоит давление 170/100."),
    ("diary", "Дневник. {fio}, {dob_gr} г.р. Принимаю {drug} 10 мг вечером, АД 135/80."),
    ("diary", "{fio}, родилась {dob_word}. Самочувствие лучше, кашель реже."),
    ("clinician_question", "Пациент {fio}, {dob_gr} г.р., паспорт {passport}. Нужна ли госпитализация?"),
    ("clinician_question", "Проверьте данные: {fio}, полис {oms}, ИНН {inn}. Всё совпадает?"),
    ("clinician_question", "{fio}, родился {dob}. СД 2 типа впервые выявлен, стартовая схема?"),
    ("insights_batch", "Пациент: 'это {fio}, {dob_gr} г.р.'. Врач: 'да, вижу карту'. Пациент: 'полис {oms} новый'."),
    ("insights_batch", "'Здравствуйте, {fio}'. 'Беспокоит {drug}' 'Какая доза?' '5 мг'. 'Оставьте пока'."),
]

# слоты, у которых вид не выводится из имени
SLOT_KIND = {
    "fio": "FIO", "fio_short": "FIO", "fio_initials": "FIO",
    "fio_lower": "FIO", "fio_r": "FIO", "fio_m": "FIO", "fio_f": "FIO",
    "fio_child": "FIO",
    "phone": "PHONE", "email": "EMAIL", "addr": "ADDRESS",
    "dob": "DOB", "dob_marked": "DOB", "dob_gr": "DOB", "dob_word": "DOB",
    "snils": "SNILS", "inn": "INN",
    "oms": "OMS", "passport": "PASSPORT", "medcard": "MEDCARD",
    "medcard_num": "MEDCARD", "age_combo": "AGE", "age_combo_short": "AGE",
    "age_age": "AGE", "age_child": "AGE",
}

# клинические decoy-даты (не ПДн): прием/начало симптомов/анализы
DECOY_DATES = ["12.03.2024", "14.05", "08.04.2025", "вчера", "02.02.2026"]


def _values(rng: random.Random, slot: str, taken: set[str]) -> str:
    """Детерминированное значение слота (уникальное в пределах контекста)."""
    for _ in range(50):
        if slot in ("fio", "fio_r", "fio_m", "fio_f"):
            v = f"{rng.choice(SUR_M if slot != 'fio_f' else SUR_F)} {rng.choice(NAME_M if slot != 'fio_f' else NAME_F)} {rng.choice(PAT_M if slot != 'fio_f' else PAT_F)}"
        elif slot == "fio_short":
            v = f"{rng.choice(SUR_M)} {rng.choice(NAME_M)}"
        elif slot == "fio_initials":
            s, n, p = rng.choice(SUR_M), rng.choice(NAME_M), rng.choice(PAT_M)
            v = f"{s} {n[0]}.{p[0]}."
        elif slot == "fio_lower":
            s, n = rng.choice(SUR_F), rng.choice(NAME_F)
            v = f"{s.lower()} {n.lower()}"
        elif slot == "fio_child":
            v = f"{rng.choice(SUR_M)} {rng.choice(NAME_M)}"
        elif slot == "phone":
            v = rng.choice(PHONES)
        elif slot == "email":
            v = rng.choice(EMAILS)
        elif slot == "addr":
            v = rng.choice(STREETS)
        elif slot in ("dob", "dob_marked"):
            v = rng.choice([d for d in DOB_FORMS if d != "9 августа 1949"])
        elif slot == "dob_gr":
            v = rng.choice([d for d in DOB_FORMS
                            if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", d)])
        elif slot == "dob_word":
            v = "9 августа 1949"
        elif slot == "snils":
            v = snils(rng.randint(100000000, 999999999))
        elif slot == "inn":
            v = inn12(str(rng.randint(1000000000, 9999999999))) if rng.random() < 0.5 \
                else inn10(str(rng.randint(100000000, 999999999)))
        elif slot == "oms":
            v = oms16(str(rng.randint(10**12, 10**13 - 1)))
        elif slot == "passport":
            v = f"{rng.randint(4000, 9999)} {rng.randint(100000, 999999)}"
        elif slot == "medcard":
            v = rng.choice(medcards())
        elif slot == "medcard_num":
            v = str(rng.randint(100000, 999999))
        elif slot in ("age_combo", "age_combo_short", "age_age"):
            a = rng.choice(AGES)
            v = f"{a} лет" if slot != "age_combo_short" else f"{a} г."
        elif slot == "age_child":
            v = str(rng.randint(2, 12))
        elif slot in ("date_clin",):
            v = rng.choice(DECOY_DATES)
        elif slot == "drug":
            v = rng.choice(["метформина", "амлодипина", "бисопролола", "лозартана",
                            "аторвастатина", "омепразола", "ибупрофена", "аспирина"])
        elif slot == "drug2":
            v = rng.choice(["варфарином", "дигоксином", "спиронолактоном",
                            "клопидогрелем", "преднизолоном"])
        else:  # pragma: no cover
            raise KeyError(slot)
        if v not in taken:
            return v
    return v


_SLOT_RE = re.compile(r"\{(\w+)\}")


def render(tpl: str, rng: random.Random) -> tuple[str, list[dict]]:
    text, gold, pos, taken = "", [], 0, set()
    for m in _SLOT_RE.finditer(tpl):
        lit = tpl[pos:m.start()]
        text += lit
        slot = m.group(1)
        v = _values(rng, slot, taken)
        taken.add(v)
        if slot not in SLOT_KIND:  # drug/drug2/date_clin — литералы, не ПДн
            text += v
            pos = m.end()
            continue
        # маркер «дата рождения» уже стоит в шаблоне — в gold входит только дата
        kind = SLOT_KIND[slot]
        start = len(text)
        text += v
        gold.append({"kind": kind, "start": start, "end": len(text), "value": v})
        pos = m.end()
    text += tpl[pos:]
    return text, gold


def main() -> None:
    rng = random.Random(20261024)
    rows: list[dict] = []
    n = 0
    # 4 варианта каждого шаблона = 196 контекстов; добираем до 200+
    for rep in range(4):
        for kind, tpl in TEMPLATES:
            n += 1
            text, gold = render(tpl, rng)
            rows.append({"id": f"cc-{n:03d}", "kind": kind, "text": text,
                         "gold": gold})
    assert len(rows) >= 150, len(rows)
    OUT.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    from collections import Counter

    print(f"written {len(rows)} contexts -> {OUT}")
    print("by kind:", dict(Counter(r["kind"] for r in rows)))
    print("by category:", dict(Counter(g["kind"] for r in rows for g in r["gold"])))


if __name__ == "__main__":
    main()
