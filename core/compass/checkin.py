"""Ежедневный адаптивный чек-ин (daily PRO): банк пунктов и детерминированная
маршрутизация.

Правила версионированы (CHECKIN_RULES_VERSION): один и тот же набор ответов при
одной и той же версии правил всегда даёт одну и ту же последовательность
пунктов. Здесь нет ИИ, рандома и «конструктора» — только таблица «если — то».

Источник формулировок, якорей и правил: docs/16-ежедневный-чек-ин.md.
Это черновик для клинического пилота: пункты не откалиброваны по IRT/Rasch и не
являются диагностическим инструментом. Риск (HR4/RS1–RS3) — отдельный маршрут
безопасности, он не суммируется с симптомами; HR4 задаётся только после ядра и
только при срабатывании safety_gate (docs/16 §2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

RULES_VERSION = "2.0.0"

#: Лимит пунктов за день: 6 пунктов ядра + gate HR4 (если задаётся) + модули.
MAX_ITEMS = 12

#: Gate-вопрос о мыслях о самоповреждении. Бинарный (0 = нет, 1 = да), не входит в сумму.
#: Задаётся не в ядре, а отдельной фазой safety и только при срабатывании safety_gate (docs/16 §2).
GATE_ITEM = "HR4"

#: Фиксированное ядро: предъявляется каждый день независимо от вчерашних модулей.
CORE_ITEMS = ("MO1", "TR1", "SL1", "IN1", "EN1", "FU1")

#: Короткий модуль безопасности после положительного gate.
SAFETY_MODULE = "RS"
SAFETY_ITEMS = ("RS1", "RS2", "RS3")

SCALE_LABELS = ("Совсем нет", "Немного", "Умеренно", "Сильно", "Очень сильно")
SCALE_EMOJI = ("🙂", "😐", "😟", "😣", "😫")
YES_NO_LABELS = ("Нет", "Да")
YES_NO_EMOJI = ("🙂", "😔")


@dataclass(frozen=True)
class CheckinItem:
    id: str
    domain: str
    text: str
    kind: str  # scale | gate | binary
    anchors: tuple[str, ...]
    emoji: tuple[str, ...]
    difficulty: str  # easy | medium | hard | n/a
    recall: str = "today"  # today | last_night


def _scale_item(id: str, domain: str, text: str, difficulty: str, recall: str = "today") -> CheckinItem:
    return CheckinItem(id, domain, text, "scale", SCALE_LABELS, SCALE_EMOJI, difficulty, recall)


ITEMS: tuple[CheckinItem, ...] = (
    _scale_item("MO1", "mood", "Сегодня я чувствовал(а) себя подавленно.", "easy"),
    _scale_item("MO2", "mood", "Сегодня мне было трудно избавиться от чувства грусти.", "medium"),
    _scale_item("MO3", "mood", "Сегодня чувство грусти сохранялось более половины дня.", "hard"),
    _scale_item("IN1", "interest", "Сегодня мне было трудно почувствовать интерес к привычным занятиям.", "easy"),
    _scale_item("IN2", "interest", "Сегодня интерес к привычным занятиям быстро пропадал.", "medium"),
    _scale_item("IN3", "interest", "Сегодня почти ничто не вызывало у меня интереса.", "hard"),
    _scale_item("TR1", "anxiety", "Сегодня я чувствовал(а) тревогу.", "easy"),
    _scale_item("TR2", "anxiety", "Сегодня мне было трудно успокоить чувство тревоги.", "medium"),
    _scale_item("TR3", "anxiety", "Сегодня тревога сохранялась более половины дня.", "hard"),
    _scale_item("RU1", "rumination", "Сегодня одни и те же неприятные мысли возвращались снова и снова.", "easy"),
    _scale_item("RU2", "rumination", "Сегодня мне было трудно переключиться с неприятных мыслей на другое.", "medium"),
    _scale_item("RU3", "rumination", "Сегодня я часами прокручивал(а) в голове одну и ту же ситуацию.", "hard"),
    CheckinItem(
        "SL1",
        "sleep",
        "Насколько отдохнувшим вы чувствовали себя после сна прошлой ночью?",
        "scale",
        ("Совсем не отдохнул(а)", "Немного", "Умеренно", "Хорошо", "Полностью отдохнул(а)"),
        ("😫", "😣", "😐", "🙂", "😴"),
        "medium",
        "last_night",
    ),
    CheckinItem(
        "SL2",
        "sleep",
        "Сколько времени вам потребовалось, чтобы заснуть прошлой ночью?",
        "scale",
        ("15 минут или меньше", "16–30 минут", "31–60 минут", "61–90 минут", "Более 90 минут"),
        ("🙂", "😐", "😟", "😣", "😫"),
        "medium",
        "last_night",
    ),
    CheckinItem(
        "SL3",
        "sleep",
        "Сколько раз прошлой ночью вы просыпались и не могли снова заснуть в течение 15 минут?",
        "scale",
        ("Ни разу", "Один раз", "Два раза", "Три раза", "Четыре раза или больше"),
        ("🙂", "😐", "😟", "😣", "😫"),
        "hard",
        "last_night",
    ),
    _scale_item("EN1", "energy", "Сегодня мне не хватало энергии.", "easy"),
    _scale_item("EN2", "energy", "Сегодня я уставал(а) быстрее, чем обычно.", "medium"),
    _scale_item("EN3", "energy", "Сегодня даже небольшие дела сильно утомляли меня.", "hard"),
    CheckinItem(
        "AP1",
        "appetite",
        "Сегодня мой аппетит был ниже обычного.",
        "scale",
        ("Не ниже обычного", "Немного ниже", "Заметно ниже", "Намного ниже", "Аппетит почти отсутствовал"),
        ("🙂", "😐", "😟", "😣", "😫"),
        "easy",
    ),
    CheckinItem(
        "AP2",
        "appetite",
        "Сегодня мой аппетит был выше обычного.",
        "scale",
        (
            "Не выше обычного",
            "Немного выше",
            "Заметно выше",
            "Намного выше",
            "Необычно сильное желание есть большую часть дня",
        ),
        ("🙂", "😐", "😟", "😣", "😫"),
        "medium",
    ),
    CheckinItem(
        "AP3",
        "appetite",
        "Сегодня отсутствие аппетита мешало мне есть.",
        "scale",
        ("Совсем не мешало", "Немного", "Умеренно", "Сильно", "Почти полностью мешало есть"),
        ("🙂", "😐", "😟", "😣", "😫"),
        "hard",
    ),
    _scale_item("FU1", "functioning", "Сегодня мне было трудно справляться с обычными повседневными делами.", "easy"),
    _scale_item("FU2", "functioning", "Сегодня мне было трудно начинать дела, которые я собирался(ась) выполнить.", "medium"),
    _scale_item("FU3", "functioning", "Сегодня мне было трудно выполнять важные для меня повседневные дела.", "hard"),
    _scale_item("SC1", "social", "Сегодня мне хотелось избегать общения.", "easy"),
    _scale_item("SC2", "social", "Сегодня мне было трудно поддерживать разговор.", "medium"),
    _scale_item("SC3", "social", "Сегодня я избегал(а) общения даже с близкими людьми.", "hard"),
    _scale_item("HR1", "hopelessness", "Сегодня я сомневался(ась), что моё состояние улучшится.", "easy"),
    _scale_item(
        "HR2",
        "hopelessness",
        "Сегодня мне было трудно представить, что в ближайшем будущем что-то изменится к лучшему.",
        "medium",
    ),
    _scale_item("HR3", "hopelessness", "Сегодня я был(а) уверен(а), что ничего не сможет измениться к лучшему.", "hard"),
    CheckinItem(
        GATE_ITEM,
        "risk",
        "Были ли у вас мысли о самоповреждении?",
        "gate",
        YES_NO_LABELS,
        YES_NO_EMOJI,
        "n/a",
    ),
    CheckinItem(
        "RS1",
        "risk",
        "Есть ли у вас мысли о самоубийстве прямо сейчас?",
        "binary",
        YES_NO_LABELS,
        YES_NO_EMOJI,
        "n/a",
    ),
    CheckinItem(
        "RS2",
        "risk",
        "Есть ли у вас намерение действовать в соответствии с этими мыслями сегодня?",
        "binary",
        YES_NO_LABELS,
        YES_NO_EMOJI,
        "n/a",
    ),
    CheckinItem(
        "RS3",
        "risk",
        "Считаете ли вы, что сможете оставаться в безопасности до получения помощи?",
        "binary",
        YES_NO_LABELS,
        YES_NO_EMOJI,
        "n/a",
    ),
)

ITEMS_BY_ID: dict[str, CheckinItem] = {item.id: item for item in ITEMS}

#: Допустимые значения ответа: индексы якорей (0..4 для шкал, 0..1 для бинарных).
MAX_VALUE: dict[str, int] = {item.id: len(item.anchors) - 1 for item in ITEMS}


@dataclass(frozen=True)
class Module:
    code: str
    title: str
    items: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ModuleDecision:
    code: str
    title: str
    items: tuple[str, ...]
    included: bool
    reason: str


@dataclass(frozen=True)
class CheckinPlan:
    """Результат маршрутизации: что спрашивать дальше и почему."""

    phase: str  # core | safety | modules | done
    items: tuple[str, ...]
    modules: tuple[ModuleDecision, ...] = ()
    safety: str = "unassessed"  # none | signal | review | unassessed
    #: Задан ли сегодня вопрос HR4; reason — основание включения или пропуска.
    safety_gate: bool = False
    safety_gate_reason: str = ""


def item(item_id: str) -> CheckinItem:
    try:
        return ITEMS_BY_ID[item_id]
    except KeyError as exc:  # pragma: no cover - защита от опечаток в правилах
        raise KeyError(f"неизвестный пункт чек-ина: {item_id}") from exc


def _answered(answers: Mapping[str, int], item_id: str) -> bool:
    return item_id in answers


def _has(answers: Mapping[str, int], item_id: str, threshold: int, *, at_least: bool) -> bool:
    value = answers.get(item_id)
    if value is None:
        return False
    return value >= threshold if at_least else value <= threshold


def _mood_or_function_heavy(a: Mapping[str, int]) -> bool:
    return _has(a, "MO1", 3, at_least=True) or _has(a, "FU1", 3, at_least=True)


def _sleep_poor(a: Mapping[str, int]) -> bool:
    return _has(a, "SL1", 2, at_least=False)


def _anxious(a: Mapping[str, int]) -> bool:
    return _has(a, "TR1", 2, at_least=True)


def _functioning(a: Mapping[str, int]) -> bool:
    return _has(a, "FU1", 2, at_least=True)


def _mood(a: Mapping[str, int]) -> bool:
    return _has(a, "MO1", 2, at_least=True)


def _interest(a: Mapping[str, int]) -> bool:
    return _has(a, "IN1", 2, at_least=True)


def _energy(a: Mapping[str, int]) -> bool:
    return _has(a, "EN1", 2, at_least=True)


def _appetite(a: Mapping[str, int]) -> bool:
    return _mood(a) or _interest(a) or _energy(a)


#: Правила ветвления в порядке приоритета (docs/16 §2). Модуль добавляется целиком.
MODULES: tuple[tuple[Module, object], ...] = (
    (
        Module("HR", "Безнадёжность", ("HR1", "HR2", "HR3"), "MO1 ≥ 3 или FU1 ≥ 3: уточнить ожидания о будущем"),
        _mood_or_function_heavy,
    ),
    (
        Module("SL", "Сон", ("SL2", "SL3"), "SL1 ≤ 2: уточнить засыпание и ночные пробуждения"),
        _sleep_poor,
    ),
    (
        Module("RU", "Руминация", ("RU1", "RU2", "RU3"), "TR1 ≥ 2: уточнить повторяющиеся мысли"),
        _anxious,
    ),
    (
        Module("FU", "Функционирование", ("FU2", "FU3"), "FU1 ≥ 2: уточнить начало и выполнение дел"),
        _functioning,
    ),
    (
        Module("MO", "Настроение", ("MO2", "MO3"), "MO1 ≥ 2: уточнить особенности и длительность сниженного настроения"),
        _mood,
    ),
    (
        Module("IN", "Интерес", ("IN2", "IN3"), "IN1 ≥ 2: уточнить устойчивость снижения интереса"),
        _interest,
    ),
    (
        Module("EN", "Энергия", ("EN2", "EN3"), "EN1 ≥ 2: уточнить утомляемость и выносливость"),
        _energy,
    ),
    (
        Module("AP", "Аппетит", ("AP1", "AP2", "AP3"), "MO1/IN1/EN1 ≥ 2: проверить изменения питания"),
        _appetite,
    ),
    (
        Module("SC", "Социальность", ("SC1", "SC2", "SC3"), "FU1 ≥ 2: уточнить социальное избегание"),
        _functioning,
    ),
)

MODULE_TITLES = {module.code: module.title for module, _ in MODULES}


def safety_level(answers: Mapping[str, int]) -> str:
    """Классификация риск-маршрута (не диагноз, не итоговая категория риска).

    `unassessed` — не «риска нет», а «риск не оценивался»: gate не задавался,
    ещё не отвечен или модуль RS не пройден до конца (docs/16 §3).
    """
    gate = answers.get(GATE_ITEM)
    if gate is None:
        return "unassessed"
    if gate != 1:
        return "none"
    if answers.get("RS1") == 1 or answers.get("RS2") == 1 or answers.get("RS3") == 0:
        return "signal"
    if _missing(SAFETY_ITEMS, answers):
        return "unassessed"
    return "review"


def _gate_trigger(answers: Mapping[str, int]) -> str:
    """Основание задать HR4; пустая строка — оснований нет."""
    if _has(answers, "MO1", 3, at_least=True):
        return "MO1 ≥ 3 (выраженное снижение настроения)"
    if _has(answers, "FU1", 3, at_least=True):
        return "FU1 ≥ 3 (выраженные трудности функционирования)"
    if _has(answers, "EN1", 2, at_least=True):
        return "EN1 ≥ 2 (заметный дефицит энергии)"
    if _module_selected(answers, "HR"):
        return "включён модуль HR (безнадёжность)"
    return ""


def _module_selected(answers: Mapping[str, int], code: str) -> bool:
    for module, condition in MODULES:
        if module.code == code:
            return bool(condition(answers))  # type: ignore[operator]
    return False


def safety_gate(answers: Mapping[str, int]) -> bool:
    """Задаётся ли сегодня HR4. После ядра решается один раз и не меняется."""
    if answers.get(GATE_ITEM) == 1:
        # Положительный ответ прошлых версий правил: сигнал нельзя потерять.
        return True
    return not _missing(CORE_ITEMS, answers) and bool(_gate_trigger(answers))


def safety_gate_reason(answers: Mapping[str, int]) -> str:
    """Причина включения или пропуска HR4 — для API, UI и аудита."""
    if answers.get(GATE_ITEM) == 1:
        return "HR4 задан: положительный ответ зафиксирован"
    if _missing(CORE_ITEMS, answers):
        return "ядро не заполнено: вопрос о риске решается после ядра"
    trigger = _gate_trigger(answers)
    if trigger:
        return f"HR4 задаётся: {trigger}"
    return "HR4 не задаётся: MO1 < 3, FU1 < 3, EN1 < 2, модуль HR не включён"


def _missing(ids: tuple[str, ...], answers: Mapping[str, int]) -> tuple[str, ...]:
    return tuple(i for i in ids if not _answered(answers, i))


def select_modules(answers: Mapping[str, int]) -> tuple[ModuleDecision, ...]:
    """Выбирает симптоматические модули: целиком, по приоритету, в пределах лимита."""
    decisions: list[ModuleDecision] = []
    budget = MAX_ITEMS - len(CORE_ITEMS) - (1 if safety_gate(answers) else 0)
    for module, condition in MODULES:
        if not condition(answers):  # type: ignore[operator]
            continue
        if len(module.items) > budget:
            decisions.append(ModuleDecision(module.code, module.title, module.items, False, "не помещается в лимит 12 пунктов"))
            continue
        budget -= len(module.items)
        decisions.append(ModuleDecision(module.code, module.title, module.items, True, module.reason))
    return tuple(decisions)


def plan(answers: Mapping[str, int]) -> CheckinPlan:
    """Следующий шаг чек-ина по уже полученным ответам.

    Ответы на модули, которые не выбраны или не предъявлены, игнорируются — но
    API обязано отклонять их до вызова plan().
    """
    core_missing = _missing(CORE_ITEMS, answers)
    gate_on = safety_gate(answers)
    gate_reason = safety_gate_reason(answers)
    if core_missing:
        return CheckinPlan("core", core_missing, (), "unassessed", gate_on, gate_reason)
    if gate_on:
        if not _answered(answers, GATE_ITEM):
            return CheckinPlan("safety", (GATE_ITEM,), (), "unassessed", True, gate_reason)
        if answers[GATE_ITEM] == 1:
            safety = safety_level(answers)
            rs_missing = _missing(SAFETY_ITEMS, answers)
            if rs_missing:
                return CheckinPlan("safety", rs_missing, (), safety, True, gate_reason)
            return CheckinPlan("done", (), (), safety, True, gate_reason)
        safety = "none"  # HR4 = «Нет»: риск явно отрицается
    else:
        # Gate не сработал: HR4 не задаётся, `unassessed` — нормальный итог дня.
        safety = "unassessed"
    modules = select_modules(answers)
    pending = tuple(i for d in modules if d.included for i in _missing(d.items, answers))
    if pending:
        return CheckinPlan("modules", pending, modules, safety, gate_on, gate_reason)
    return CheckinPlan("done", (), modules, safety, gate_on, gate_reason)


def planned_items(answers: Mapping[str, int]) -> tuple[str, ...]:
    """Полный список пунктов дня (для аудита и прогресса) — без учёта пропусков."""
    if answers.get(GATE_ITEM) == 1:
        return CORE_ITEMS + (GATE_ITEM,) + SAFETY_ITEMS
    gate_on = safety_gate(answers)
    if gate_on and not _answered(answers, GATE_ITEM):
        return CORE_ITEMS + (GATE_ITEM,)
    modules = select_modules(answers)
    head = CORE_ITEMS + ((GATE_ITEM,) if gate_on else ())
    return head + tuple(i for d in modules if d.included for i in d.items)


def validate_answers(answers: Mapping[str, int]) -> None:
    """Проверка диапазонов; неизвестные пункты и пропуски проверяет API."""
    for item_id, value in answers.items():
        if item_id not in MAX_VALUE:
            raise ValueError(f"неизвестный пункт чек-ина: {item_id}")
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_VALUE[item_id]:
            raise ValueError(f"значение {value!r} для {item_id} вне диапазона 0..{MAX_VALUE[item_id]}")


__all__ = [
    "RULES_VERSION",
    "MAX_ITEMS",
    "GATE_ITEM",
    "CORE_ITEMS",
    "SAFETY_ITEMS",
    "ITEMS_BY_ID",
    "MAX_VALUE",
    "CheckinItem",
    "CheckinPlan",
    "Module",
    "ModuleDecision",
    "item",
    "plan",
    "planned_items",
    "safety_level",
    "safety_gate",
    "safety_gate_reason",
    "select_modules",
    "validate_answers",
]
