"""Валидированные шкалы. Пороги severity подтверждает медик команды [TBD].

PHQ-9: Kroenke et al. 2001; GAD-7: Spitzer et al. 2006; HADS: Zigmond & Snaith 1983
(русская адаптация Андрющенко–Дробижев). Русские тексты — валидированные версии.
"""

from __future__ import annotations

from compass.models import Scale, ScaleItem, Severity

PHQ9 = Scale(
    code="PHQ-9",
    title="Шкала депрессии PHQ-9",
    items=(
        ScaleItem("q1", "Мало интереса или удовольствия от того, что я делаю"),
        ScaleItem("q2", "Подавленность, угнетённость или безнадёжность"),
        ScaleItem("q3", "Проблемы с засыпанием, прерывистый сон или слишком долгий сон"),
        ScaleItem("q4", "Усталость или недостаток сил"),
        ScaleItem("q5", "Плохой аппетит или переедание"),
        ScaleItem("q6", "Плохое мнение о себе — я считаю себя неудачником(цей), подвожу себя и свою семью"),
        ScaleItem("q7", "Трудности с концентрацией внимания (например, при чтении или просмотре телевизора)"),
        ScaleItem("q8", "Замедленность или, наоборот, суетливость, беспокойство"),
        ScaleItem("q9", "Мысли о том, что было бы лучше умереть, или о причинении себе боли"),
    ),
    severity_bounds=(
        (0, Severity.NONE),
        (5, Severity.MILD),
        (10, Severity.MODERATE),
        (15, Severity.SEVERE),
    ),
    suicidal_item="q9",
)

GAD7 = Scale(
    code="GAD-7",
    title="Шкала генерализованной тревоги GAD-7",
    items=(
        ScaleItem("q1", "Нервозность, тревожность или ощущение, что всё «на пределе»"),
        ScaleItem("q2", "Невозможность перестать беспокоиться или контролировать беспокойство"),
        ScaleItem("q3", "Чрезмерное беспокойство по разным поводам"),
        ScaleItem("q4", "Трудности с расслаблением"),
        ScaleItem("q5", "Беспокойство, из-за которого трудно усидеть на месте"),
        ScaleItem("q6", "Раздражительность или вспыльчивость"),
        ScaleItem("q7", "Чувство страха, будто может случиться что-то ужасное"),
    ),
    severity_bounds=(
        (0, Severity.NONE),
        (5, Severity.MILD),
        (10, Severity.MODERATE),
        (15, Severity.SEVERE),
    ),
)

HADS_A = Scale(
    code="HADS-A",
    title="Госпитальная шкала тревоги и депрессии: подшкала тревоги (HADS-A)",
    items=(
        ScaleItem("a1", "Я испытываю напряжённость, мне не по себе"),
        ScaleItem("a2", "Мне страшно, кажется, будто что-то ужасное может вот-вот случиться"),
        ScaleItem("a3", "Беспокойные мысли крутятся у меня в голове"),
        ScaleItem("a4", "Я легко могу сесть и расслабиться"),
        ScaleItem("a5", "Я испытываю внутреннее напряжение или дрожь"),
        ScaleItem("a6", "Я испытываю двигательное беспокойство, мне не сидится на месте"),
        ScaleItem("a7", "Меня внезапно охватывает паника"),
    ),
    severity_bounds=(
        (0, Severity.NONE),
        (8, Severity.MILD),
        (11, Severity.MODERATE),
    ),
)

HADS_D = Scale(
    code="HADS-D",
    title="Госпитальная шкала тревоги и депрессии: подшкала депрессии (HADS-D)",
    items=(
        ScaleItem("d1", "То, что раньше приносило мне удовольствие, и сейчас приносит мне его", reverse=True),
        ScaleItem("d2", "Я способен(на) рассмеяться и увидеть в том или ином событии смешное", reverse=True),
        ScaleItem("d3", "Я чувствую себя бодрым(ой)", reverse=True),
        ScaleItem("d4", "Мне кажется, что я стал(а) всё делать очень медленно"),
        ScaleItem("d5", "Я слежу за своей внешностью", reverse=True),
        ScaleItem("d6", "Я жду, что получу удовольствие от своих любимых занятий", reverse=True),
        ScaleItem("d7", "Я могу получить удовольствие от хорошей книги, радио- или телепередачи", reverse=True),
    ),
    severity_bounds=(
        (0, Severity.NONE),
        (8, Severity.MILD),
        (11, Severity.MODERATE),
    ),
)

ALL_SCALES: dict[str, Scale] = {s.code: s for s in (PHQ9, GAD7, HADS_A, HADS_D)}

__all__ = ["PHQ9", "GAD7", "HADS_A", "HADS_D", "ALL_SCALES"]