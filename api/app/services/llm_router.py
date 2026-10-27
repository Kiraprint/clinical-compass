"""LLM-роутер раздела БЗ для ассистента врача (docs/04 FR-14, docs/08).

Вместо ручного выбора «Раздела» в UI (удалён) топик выбирает LLM: запрос
отправляется на OpenAI-совместимый /v1/chat/completions с набором тулз
search_<ТОПИК>, модель обязана вернуть ровно один tool_call, из имени тулзы
выводится topic.

Три вызова LLM, у каждого своя узкая роль:
route_topic (FR-14) выбирает раздел БЗ, rewrite_followup (FR-21) разворачивает
уточняющий вопрос треда («а в каких дозах?») в самодостаточный поисковый запрос,
compose_answer (FR-17) пишет ответ по-русски по уже найденным сниппетам.
Клинические границы у всех трёх общие: модель не ставит диагноз, не назначает
терапию и не подбирает дозы (красные линии docs/01), а отказные пути всегда
детерминированные.

Границы (docs/12): на вход LLM подаётся только очищенный от ПДн текст
(scrub_for_llm вызывается в routers/assistant.py и assistant_chat.py до вызова).
Карта выбранного пациента уходит в compose_answer/_stream отдельным блоком
`Карта пациента (обезлично):` — уже после scrub_for_llm (`fio` → `[ПАЦИЕНТ]`,
дневник маскирован), без `patient_id` и контактов; см. `_format_patients`,
docs/07 §7.1 и риск R35 (docs/10).

Отказоустойчивость: URL не задан / таймаут / ошибка HTTP / пустой ответ →
route_topic возвращает None (= поиск по всем разделам), rewrite_followup —
None (= поиск по исходному вопросу), compose_answer — None (= детерминированный
build_rag_answer). Ответ не ломается.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import AsyncIterator

import anyio
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Переформулировка уточняющего вопроса (FR-21): границы истории, уходящей в LLM.
REWRITE_HISTORY_MESSAGES = 8      # последних сообщений треда
REWRITE_MESSAGE_CHARS = 800       # обрезка одного сообщения
REWRITE_HISTORY_CHARS = 3000      # обрезка всей истории (с конца, свежее важнее)
REWRITE_QUERY_CHARS = 800         # обрезка текущего вопроса и переформулировки

# Ответ по найденным фрагментам (FR-17): границы сниппетов, уходящих в LLM.
ANSWER_SNIPPETS = 5               # не больше, чем отдаёт поиск (limit=5)
ANSWER_SNIPPET_CHARS = 700        # обрезка одного сниппета
ANSWER_SNIPPETS_CHARS = 3500      # обрезка всех сниппетов разом
# Карта пациента в промпте ответа (FR-17, docs/07 §7): модель видит ТОЛЬКО
# маскированную карту (fio → [ПАЦИЕНТ], дневник прошёл scrub_for_llm) и
# ссылается на неё как [П1]. patient_id и ФИО в текст карты не попадают.
ANSWER_PATIENTS = 3                # не больше карт в промпте (обычно одна)
PATIENT_CARD_CHARS = 1800          # обрезка одной карты (FR-50: факты карточки)
PATIENT_CARDS_CHARS = 4200         # обрезка всех карт разом
PATIENT_DIARY_ENTRIES = 3          # последних записей дневника на пациента
# Разделы расширенной карты (FR-50): бюджет каждого раздела отдельно, чтобы
# длинный анамнез не вытеснял препараты и нежелательные реакции.
PATIENT_ANAMNESIS_CHARS = 600
PATIENT_MEDS_CHARS = 500
PATIENT_AE_CHARS = 300
PATIENT_INSIGHTS_CHARS = 300
PATIENT_VISITS_CHARS = 300
# Минимум на раздел при перераспределении общего бюджета: заголовок и начало
# текста — модель должна видеть, что раздел в карте есть.
_SECTION_MIN_CHARS = 40
PATIENT_CARD_HEADER = "Карта пациента (обезлично):"
# Маска ФИО: то же значение, что кладёт scrub_for_llm (services.agent).
PATIENT_MASK = "[ПАЦИЕНТ]"


# Топики корпуса БЗ (docs/08): ключ = имя тулзы, значение = topic в Qdrant.
# Тулза search_all специально не нужна: model может не вызывать тулзу вовсе
# (tool_choice: auto), и тогда topic = None → поиск по всем разделам.
TOPIC_TOOLS = {
    "search_skaly": "ШКАЛЫ",
    "search_triazh": "ТРИАЖ",
    "search_rekomendacii": "РЕКОМЕНДАЦИИ",
    "search_lekarstva": "ЛЕКАРСТВА",
    "search_nauchnye_stati": "НАУЧНЫЕ-СТАТЬИ",
}


def _tools_schema() -> list[dict]:
    """OpenAI-схема тулз: одна функция на раздел БЗ, аргумент — только запрос."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": (
                    f"Поиск по разделу базы знаний «{topic}» "
                    f"({_TOPIC_HINT[topic]}). Вызови, если вопрос относится к этому разделу."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Формулировка запроса пользователя (можно без изменений)",
                        }
                    },
                    "required": ["query"],
                },
            },
        }
        for name, topic in TOPIC_TOOLS.items()
    ]


_TOPIC_HINT = {
    "ШКАЛЫ": "шкалы, пороги, вопросы PHQ-9/GAD-7/HADS",
    "ТРИАЖ": "триаж, GREEN/YELLOW/RED, эскалация, суицидальный риск",
    "РЕКОМЕНДАЦИИ": "клинические рекомендации Минздрава, лечение, ведение",
    "ЛЕКАРСТВА": "препараты, дозировки, показания, побочные эффекты",
    "НАУЧНЫЕ-СТАТЬИ": "систематические обзоры и метаанализы PubMed, оценка методологии",
}

_SYSTEM_PROMPT = (
    "Ты — маршрутизатор запросов к базе знаний клинического помощника врача. "
    "Определи, к каким разделам относится вопрос пользователя, и вызови тулзу "
    "поиска по каждому из них. Обычно раздел один. Вызови две тулзы, только если "
    "вопрос явно про них обе: например, про препарат и одновременно про "
    "исследования/доказательства/обзоры (search_lekarstva + "
    "search_nauchnye_stati), или про шкалу и триаж. Больше двух тулз не вызывай. "
    "Если вопрос не относится ни к одному разделу — не вызывай тулзу. "
    "Никогда не отвечай текстом от себя: только вызов тулз (или ничего)."
)


def _enabled() -> bool:
    return bool(settings.assistant_llm_url.strip())



def _thinking(payload: dict) -> dict:
    """Отключение «размышлений» модели в теле /chat/completions.

    Два разных механизма, и оба нужны:
    * `chat_template_kwargs.enable_thinking` — понимает llama.cpp;
    * `reasoning_effort` — **полем верхнего уровня**, как в OpenAI API; внутри
      `chat_template_kwargs` шлюз (omniroute) его не читает, и CoT остаётся.

    Замер с потоком по реальным сниппетам (`home-stable` → `ds/deepseek-v4-flash`):
    ключ внутри `chat_template_kwargs` — reasoning-контент всё равно идёт
    (185-378 символов, первый токен 12-25 с); `reasoning_effort=none` верхним
    уровнем — reasoning ровно 0 символов и первый токен 6.9-17.6 с (разброс —
    очередь шлюза). `low` не помогает: рассуждение не ограничивается по длине.
    Значение пустое → ключ не отправляем (llama.cpp его не знает).
    """
    payload["chat_template_kwargs"] = {"enable_thinking": False}
    effort = settings.assistant_llm_reasoning_effort.strip()
    if effort:
        payload["reasoning_effort"] = effort
    return payload

def _request(payload: dict, *, timeout: float | None = None) -> dict:
    """POST /chat/completions к OpenAI-совместимому шлюзу.

    timeout — переопределение settings.assistant_llm_timeout для длинных
    задач (FR-31: извлечение фактов отвечает 12–13 с и иногда дольше).
    Исключения urllib/json не ловим — их обрабатывает вызывающий (fallback без LLM).
    """
    data = json.dumps(payload).encode("utf-8")
    url = settings.assistant_llm_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    api_key = settings.assistant_llm_api_key.strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(
        req, timeout=timeout or settings.assistant_llm_timeout
    ) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat_completion(
    messages: list[dict], *, temperature: float = 0.0, timeout: float | None = None
) -> dict:
    """Сырой POST /chat/completions для других LLM-задач (FR-31, FR-37, FR-44).

    Транспорт общий с роутером топиков: таймаут/ключ/URL берутся из settings.
    Ключи отключения CoT (`_thinking`) сюда НЕ добавляются осознанно: это задачи
    вне ассистента (анализ переписки, анамнез, диалог по сессии), и менять их
    промпт-режим этим PR не нужно — иначе `reasoning_effort=none` молча меняет
    поведение четырёх фич. Ошибки транспорта и невалидный JSON не ловим — их
    обрабатывает вызывающий.
    """
    payload: dict = {
        "model": settings.assistant_llm_model,
        "messages": messages,
        "temperature": temperature,
    }
    return _request(payload, timeout=timeout)


def chat_with_tools(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    temperature: float = 0.0,
    timeout: float | None = None,
) -> dict:
    """Один раунд tool-loop диалога по сессии (FR-44, docs/18 §10).

    Отличие от `chat_completion`: набор тулз и `tool_choice: auto`. `tools=None`
    — финальный проход без тулз, когда раунды вызовов исчерпаны: модель обязана
    вернуть текст, а не ещё один вызов. `enable_thinking: false` — reasoning-
    модели иначе тратят бюджет на CoT вместо ответа (как в route_topics).
    Ошибки транспорта не ловим: их обрабатывает вызывающий (детерминированный
    отказ вместо падения ручки).
    """
    payload: dict = {
        "model": settings.assistant_llm_model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return _request(_thinking(payload), timeout=timeout)


MAX_TOPICS = 2  # больше двух разделов на один вопрос — это уже «поиск по всем»


def route_topics(query: str, *, timeout: float | None = None) -> tuple[str, ...]:
    """Выбирает 1–2 темы (ШКАЛЫ/ТРИАЖ/РЕКОМЕНДАЦИИ/ЛЕКАРСТВА/НАУЧНЫЕ-СТАТЬИ).

    Пустой кортеж = поиск по всем разделам (LLM выключен/ошибка/вопрос вне
    разделов). Модель может вызвать две тулзы: вопрос «препарат X и его
    доказательства» относится и к ЛЕКАРСТВАМ, и к НАУЧНЫМ-СТАТЬЯМ. Оба фильтра
    уходят в Qdrant одним запросом (MatchAny), поэтому лишних поисков нет.
    """
    # timeout — остаток бюджета прогона (CC_ASSISTANT_DEADLINE_S), не больше
    # settings.assistant_llm_timeout: роутер и ответ идут последовательно, и
    # второй вызов обязан уложиться в то, что осталось от дедлайна.
    if not _enabled():
        return ()
    if not query.strip():
        return ()
    try:
        payload = {
            "model": settings.assistant_llm_model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            "tools": _tools_schema(),
            "tool_choice": "auto",
            "temperature": 0.0,
            # max_tokens не передаём: reasoning-модели тратят бюджет на thinking
            # до tool_call, и любой наш лимит возвращал finish_reason=length без
            # выбора раздела. Потолок задаёт сам сервер/шлюз по модели.
            # enable_thinking + reasoning_effort верхнего уровня — см. _thinking.
        }
        body = _request(_thinking(payload), timeout=timeout)
        msg = body["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return ()  # вне разделов — поиск по всем
        topics: list[str] = []
        for call in tool_calls:
            fn = call.get("function", {}).get("name", "")
            topic = TOPIC_TOOLS.get(fn)
            if topic is None:
                logger.warning("assistant LLM вернул неизвестную тулзу %r — пропускаю", fn)
                continue
            if topic not in topics:
                topics.append(topic)
            if len(topics) >= MAX_TOPICS:
                break
        return tuple(topics)
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("assistant LLM-роутинг недоступен (%s: %s) — поиск по всем", type(exc).__name__, exc)
        return ()


def route_topic(query: str) -> str | None:
    """Первая (основная) тема — для вызовов, которым нужен один раздел."""
    topics = route_topics(query)
    return topics[0] if topics else None


_REWRITE_SYSTEM_PROMPT = (
    "Ты помогаешь врачу искать по базе знаний. Тебе дают предыдущие реплики "
    "диалога и последний вопрос. Переформулируй последний вопрос так, чтобы он "
    "был понятен без истории: подставь из предыдущих реплик предмет обсуждения "
    "(например, препарат, шкалу, состояние). Верни ТОЛЬКО самодостаточный вопрос "
    "одной строкой — без пояснений, без кавычек, без нумерации. Если последний "
    "вопрос уже самодостаточен, верни его без изменений."
)

_ROLE_LABELS = {"CLINICIAN": "Врач", "ASSISTANT": "Ассистент"}
# Модель иногда всё же префиксует ответ («Вопрос: …») — снимаем.
_REWRITE_PREFIX = re.compile(r"^(?:переформулированн\w+ вопрос|вопрос|запрос|ответ)\s*:\s*", re.I)

# FR-17: клинический ответ пишет модель, но только по найденным сниппетам и
# обезличенной карте пациента. Запреты продублированы в коде (red line
# считается ДО вызова LLM): промпт — не единственный барьер, а ПДн в промпт
# не попадают: карта уходит уже после scrub_for_llm (fio → [ПАЦИЕНТ]).
_ANSWER_SYSTEM_PROMPT = (
    "Ты — справочный ассистент врача. Отвечай по-русски, связно и по делу, "
    "только на основе пронумерованных сниппетов базы знаний из сообщения "
    "пользователя и с учётом истории диалога.\n"
    "Правила:\n"
    "1. Не ставь диагноз, не назначай лечение, не подбирай дозы и схемы приёма — "
    "это функция врача.\n"
    "2. Не добавляй факты, которых нет в сниппетах или в карте пациента: не "
    "выдумывай числа, названия препаратов, шкал и источников.\n"
    "3. Каждое утверждение подкрепляй ссылкой на сниппет в виде [n], где n — "
    "номер сниппета; допустимо несколько ссылок подряд: [1][2].\n"
    "4. Если сниппетов и карты пациента не хватает для ответа, прямо скажи, что "
    "в базе знаний нет нужных данных, и перечисли, что удалось найти.\n"
    "5. В карте пациента персональные данные обезличены (ФИО заменено на "
    "[ПАЦИЕНТ]) — не пытайся их восстановить, не упоминай и не запрашивай.\n"
    "6. Верни только текст ответа: без markdown-заголовков и служебных "
    "комментариев.\n"
    "7. Отвечай кратко: обычно 3–6 предложений; перечисление — только если "
    "вопрос просит список. Не повторяй сниппеты целиком.\n"
    "8. Если в сообщении есть блок «Карта пациента (обезлично)», используй её "
    "факты вместе со сниппетами: баллы, тяжесть, триаж, тренд, эскалации и "
    "дневник — это данные пациента врача, вопрос обычно про них.\n"
    "9. На карту ссылайся как [П1], [П2] — номер карты в том же блоке (П1 — "
    "первый пациент). Ссылку ставь в конце утверждения, взятого из карты.\n"
    "10. Поля, которых нет в карте, не выдумывай: если нужного числа (пол, "
    "возраст, балл, дата) в карте нет, так и скажи; диагноз и дозы не "
    "назначай даже при наличии карты.\n"
    "11. Препараты, анамнез, нежелательные реакции, факты из чата и визиты "
    "бери из блока карты и ссылайся [Пn]; если такого раздела в карте нет — "
    "скажи «в карте не заполнено» и не додумывай."
)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip()


def _format_history(history: list[dict]) -> str:
    """Последние REWRITE_HISTORY_MESSAGES реплик в пределах бюджета символов.

    Свежие реплики важнее старых: при переполнении бюджета отбрасываем начало.
    """
    lines: list[str] = []
    used = 0
    for m in reversed(history[-REWRITE_HISTORY_MESSAGES:]):
        content = _clip(str(m.get("content") or ""), REWRITE_MESSAGE_CHARS)
        if not content:
            continue
        label = _ROLE_LABELS.get(str(m.get("role")), "Врач")
        line = f"{label}: {content}"
        if used + len(line) > REWRITE_HISTORY_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(reversed(lines))


def _clean_rewrite(text: str) -> str | None:
    """Ответ модели → одна строка без markdown/кавычек/префикса. None = мусор.

    Многострочный ответ режем до первой непустой строки: пояснения модели не
    должны попадать в embedding (они уводят поиск от вопроса врача).
    """
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = line.strip("`*_ \t«»\"'“”„")
    line = _REWRITE_PREFIX.sub("", line)
    line = _clip(line, REWRITE_QUERY_CHARS).strip(" `*_«»\"'“”„")
    return line or None


def rewrite_followup(history: list[dict], query: str) -> str | None:
    """Разворачивает уточняющий вопрос треда в самодостаточный (FR-21).

    history — предыдущие реплики треда [{"role": "CLINICIAN"|"ASSISTANT",
    "content": str}], уже деперсонализированные (scrub_for_llm); query —
    текущий вопрос, тоже деперсонализированный. Пустая история → None без
    сетевого вызова: одиночный вопрос переформулировать не нужно.

    None = переформулировка не нужна/недоступна → поиск по исходному вопросу.
    Результат используется только для retrieval и выбора раздела, не для
    ответа, цитат и red line (те считаются по исходному вопросу).
    """
    if not _enabled() or not history or not query.strip():
        return None
    formatted = _format_history(history)
    if not formatted:
        return None
    try:
        payload: dict = {
            "model": settings.assistant_llm_model,
            "messages": [
                {"role": "system", "content": _REWRITE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"История диалога:\n{formatted}\n\n"
                        f"Последний вопрос: {_clip(query, REWRITE_QUERY_CHARS)}"
                    ),
                },
            ],
            "temperature": 0.0,
        }
        body = _request(_thinking(payload))
        rewritten = _clean_rewrite(body["choices"][0]["message"].get("content") or "")
        if rewritten is None:
            logger.warning("assistant LLM вернул пустую переформулировку — исходный вопрос")
        return rewritten
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning(
            "assistant LLM-переформулировка недоступна (%s: %s) — исходный вопрос",
            type(exc).__name__,
            exc,
        )
        return None


def _format_snippets(chunks: list[dict]) -> tuple[str, int]:
    """Найденные фрагменты как нумерованный список для промпта.

    Номера — позиции в chunks (1-based), те же, что потом проверяет сервер:
    ссылка [n] в ответе модели означает chunks[n - 1]. Пустой текст фрагмента
    строку не даёт, но нумерацию не сдвигает.

    Возвращает (текст, наибольший номер сниппета): всё, что за ним (лимит
    ANSWER_SNIPPETS или бюджет символов), модель не видела — ссылаться на это
    нельзя, поэтому сервер валидирует [n] по этому окну, а не по len(chunks).
    Второй элемент 0 — в промпт не попал ни один фрагмент.
    """
    lines: list[str] = []
    used = 0
    window = 0
    for i, chunk in enumerate(chunks[:ANSWER_SNIPPETS], start=1):
        text = _clip(" ".join(str(chunk.get("text") or "").split()), ANSWER_SNIPPET_CHARS)
        if not text:
            continue
        title = _clip(str(chunk.get("source_title") or "База знаний"), 120)
        line = f"[{i}] {title}: {text}"
        if used + len(line) > ANSWER_SNIPPETS_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
        window = i
    return "\n".join(lines), window


def answer_snippet_window(chunks: list[dict]) -> int:
    """Наибольший номер сниппета, который реально видит модель в compose_answer.

    Верхняя граница для ссылок [n]: сниппеты после ANSWER_SNIPPETS или за
    бюджетом символов в промпт не попадают, и ссылка на них — выдумка модели.
    """
    return _format_snippets(chunks)[1]


# Свободный текст разделов расширенной карты (FR-50). Печатается одной строкой
# на раздел: модель отвечает списком, а не пересказом карточки.
def _fit_sections(sections: list[str], budget: int | None) -> list[str]:
    """Ужимает разделы карты под общий бюджет, не выбрасывая ни один заголовок.

    Сумма потолков разделов (500+600+300+300+300) больше `PATIENT_CARD_CHARS`:
    если каждый раздел упрётся в свой потолок, хвостовой `_clip` срезал бы
    «визиты» целиком. Поэтому остаток бюджета делится пропорционально длинам —
    раздел остаётся виден, теряя только хвост текста.
    """
    if budget is None:
        return sections
    total = sum(len(section) for section in sections)
    if total <= budget:
        return sections
    fitted = []
    for section in sections:
        share = max(_SECTION_MIN_CHARS, int(budget * len(section) / total))
        fitted.append(_clip(section, share))
    return fitted


def _patient_fact_bits(ctx: dict, budget: int | None = None) -> list[str]:
    """Разделы карты пациента: препараты, анамнез, НЯ, факты из чата, приёмы.

    Пустой раздел не печатается вовсе: строка «препараты: —» сказала бы модели,
    что препаратов нет, хотя врач их просто не внёс — а ассистент обязан
    различать «нет данных» и «нет в карте» (правило 11 системного промпта).
    `budget` — сколько символов осталось на все разделы в строке карты.
    """
    bits: list[str] = []

    meds = ctx.get("medications") or []
    if meds:
        items = []
        for m in meds:
            head = " ".join(
                part for part in (
                    " ".join(str(m.get("title") or "").split()) or "—",
                    " ".join(str(m.get("dosage") or "").split()),
                ) if part
            )
            meta = [str(m.get("status") or "статус не указан")]
            if m.get("times_per_day"):
                meta.append(f"{m['times_per_day']} р/сут")
            if m.get("started_on"):
                meta.append(f"с {str(m['started_on'])[:10]}")
            item = f"{head} [{', '.join(meta)}]"
            notes = " ".join(str(m.get("notes") or "").split())
            if notes:
                item += f" — {notes}"
            items.append(item)
        bits.append(f"препараты: {_clip(' | '.join(items), PATIENT_MEDS_CHARS)}")

    anamnesis = ctx.get("anamnesis") or {}
    brief = " ".join(str(anamnesis.get("brief") or "").split())
    if brief:
        if anamnesis.get("updated_at"):
            brief += f" (обновлён {str(anamnesis['updated_at'])[:10]})"
        bits.append(f"анамнез: {_clip(brief, PATIENT_ANAMNESIS_CHARS)}")

    adverse = ctx.get("adverse_events") or []
    if adverse:
        items = []
        for e in adverse:
            text = " ".join(str(e.get("event_text") or "").split()) or "—"
            meta = [str(e.get("severity") or "тяжесть не указана")]
            if e.get("onset_date"):
                meta.append(f"от {str(e['onset_date'])[:10]}")
            if e.get("suspected_drug"):
                meta.append(f"подозреваемый препарат: {e['suspected_drug']}")
            if e.get("action_taken"):
                meta.append(f"действия: {e['action_taken']}")
            items.append(f"{text} ({', '.join(meta)})")
        bits.append(f"нежелательные реакции: {_clip(' | '.join(items), PATIENT_AE_CHARS)}")

    insights = ctx.get("chat_insights") or []
    if insights:
        items = []
        for i in insights:
            text = " ".join(str(i.get("text") or "").split())
            if text:
                items.append(f"{i.get('kind') or 'факт'}: {text}")
        if items:
            bits.append(f"факты из чата: {_clip(' | '.join(items), PATIENT_INSIGHTS_CHARS)}")

    visits = ctx.get("visits") or []
    if visits:
        items = []
        for v in visits:
            item = " ".join(
                part for part in (
                    str(v.get("visited_at") or "")[:10],
                    str(v.get("visit_type") or ""),
                ) if part
            )
            notes = " ".join(str(v.get("notes") or "").split())
            if notes:
                item += f": {notes}"
            items.append(item)
        bits.append(f"визиты: {_clip(' | '.join(items), PATIENT_VISITS_CHARS)}")
    return _fit_sections(bits, budget)


def _patient_line(index: int, ctx: dict) -> str:
    """Одна карта пациента строкой `П<n>: пациент: [ПАЦИЕНТ]; ...`.

    ФИО в строку не переносится никогда: если карта пришла уже скрабленной
    (scrub_for_llm → [ПАЦИЕНТ]), печатается маска; если в fio пришло что-то
    другое, это тоже маска — сырое имя в промпт не уходит ни при каком
    вызывающем. patient_id также не печатается (он есть только в citation).
    Числа (баллы, тренд, даты) идут как есть: модель обязана опираться на них,
    а не пересчитывать. Отсутствующие значения — `—`, а не выдуманные.
    """
    last = ctx.get("last_assessment") or {}
    trend = ctx.get("trend") or {}
    esc = ctx.get("open_escalations") or []
    diary = (ctx.get("recent_diary") or [])[:PATIENT_DIARY_ENTRIES]
    scales = ctx.get("prescribed_scales") or ctx.get("prescribed_scale") or []
    if isinstance(scales, str):
        scales = [scales]
    bits = [
        f"пациент: {PATIENT_MASK if str(ctx.get('fio') or '').strip() else 'без ФИО'}",
        f"пол: {ctx.get('sex') or '—'}",
        f"возраст: {ctx.get('age') if ctx.get('age') is not None else '—'}",
        f"шкалы: {', '.join(str(s) for s in scales) or '—'}",
    ]
    if last:
        bits.append(
            f"последняя оценка: {last.get('total_score')} ({last.get('severity')})"
            + (f" от {str(last['computed_at'])[:10]}" if last.get("computed_at") else "")
        )
        bits.append(f"триаж: {last.get('triage') or '—'}")
    else:
        bits.append("последняя оценка: нет; триаж: —")
    avg = trend.get("moving_avg") or []
    trend_bits: list[str] = []
    if avg:
        trend_bits.append(f"среднее {round(float(avg[-1]), 1)}")
    if trend.get("delta_7") is not None:
        trend_bits.append(f"дельта 7д {trend['delta_7']}")
    if trend.get("delta_30") is not None:
        trend_bits.append(f"дельта 30д {trend['delta_30']}")
    if trend.get("slope") is not None:
        trend_bits.append(f"наклон {round(float(trend['slope']), 2)}")
    bits.append(f"тренд: {', '.join(trend_bits) or '—'}")
    bits.append(
        f"открытые эскалации: {len(esc)}"
        + (f" ({', '.join(str(e.get('kind') or '?') for e in esc)})" if esc else "")
    )
    entries = [
        _clip(" ".join(str(d.get("content") or "").split()), 120)
        for d in diary
        if str(d.get("content") or "").strip()
    ]
    # Факты карточки идут до дневника: бюджет карты при переполнении срезает
    # хвост строки, и свободный текст дневника не должен вытеснять препараты.
    # Разделам достаётся то, что осталось после базы и дневника.
    diary_text = f"дневник: {' | '.join(entries) if entries else '—'}"
    prefix = f"П{index}: " + "; ".join(bits)
    # Запас на разделители "; " между разделами (их не больше пяти) и запись дневника.
    budget = PATIENT_CARD_CHARS - len(prefix) - len(diary_text) - 12
    bits.extend(_patient_fact_bits(ctx, max(0, budget)))
    bits.append(diary_text)
    return f"П{index}: " + "; ".join(bits)


def _format_patients(contexts: list[dict] | None) -> tuple[str, list[dict]]:
    """Карты пациентов блоком для промпта; (текст, реально отправленные карты).

    Возвращает ("", []), если карт нет — тогда блок в тело запроса не
    добавляется. Второй элемент — не «сколько», а сами отправленные карты:
    номер `П<n>` — позиция в этом списке, поэтому citation по `[П<n>]`
    собирается из тех же данных, что видела модель, даже если входной список
    содержал пустые элементы или не влез в ANSWER_PATIENTS/бюджет символов.
    """
    lines: list[str] = []
    used = 0
    sent: list[dict] = []
    for ctx in contexts or []:
        if len(sent) >= ANSWER_PATIENTS:
            break
        if not ctx:
            continue
        line = _clip(_patient_line(len(sent) + 1, ctx), PATIENT_CARD_CHARS)
        if used + len(line) > PATIENT_CARDS_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
        sent.append(ctx)
    return "\n".join(lines), sent


def answer_patients(contexts: list[dict] | None) -> list[dict]:
    """Карты, которые реально видит модель: номер `[П<n>]` = индекс здесь + 1."""
    return _format_patients(contexts)[1]


def clean_answer(text: str) -> str | None:
    """Ответ модели → текст без markdown-обёртки. None = пусто/мусор."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    return text or None


def _answer_body(
    history: list[dict],
    query: str,
    chunks: list[dict],
    patient_contexts: list[dict] | None = None,
) -> dict | None:
    """Тело /chat/completions для ответа по сниппетам (общее с потоком).

    patient_contexts — уже деперсонализированные карты (assistant_chat зовёт
    scrub_for_llm выше по стеку). Блок карты идёт ПОСЛЕ сниппетов: модель
    видит, что первичный корпус — база знаний, а карта лишь уточняет контекст
    вопроса. Без карт блок не добавляется вовсе — тело прежнее байт-в-байт.
    """
    if not _enabled() or not query.strip() or not chunks:
        return None
    snippets, _window = _format_snippets(chunks)
    if not snippets:
        return None
    parts: list[str] = []
    formatted = _format_history(history or [])
    if formatted:
        parts.append(f"История диалога:\n{formatted}")
    parts.append(f"Вопрос врача: {_clip(query, REWRITE_QUERY_CHARS)}")
    parts.append(f"Сниппеты базы знаний:\n{snippets}")
    cards, _sent = _format_patients(patient_contexts)
    if cards:
        parts.append(f"{PATIENT_CARD_HEADER}\n{cards}")
    payload: dict = {
        "model": settings.assistant_llm_model,
        "messages": [
            {"role": "system", "content": _ANSWER_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ],
        # 0.2 — связная формулировка, но без «творчества»: факты и
        # ссылки берутся из сниппетов, повторяемость важна для тестов.
        "temperature": 0.2,
    }
    return _thinking(payload)


def _llm_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = settings.assistant_llm_api_key.strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


class TruncatedAnswer(Exception):
    """Апстрим оборвал генерацию: текст неполный (finish_reason=length или
    поток закрылся без терминального чанка).

    Усечённый клинический ответ нельзя отдавать как ответ ИИ — вызывающий
    деградирует до детерминированной выдачи фрагментов (reason "truncated").
    """


def compose_answer_result(
    history: list[dict],
    query: str,
    chunks: list[dict],
    *,
    timeout: float | None = None,
    patient_contexts: list[dict] | None = None,
) -> tuple[str | None, str]:
    """Ответ ассистента по найденным фрагментам и истории диалога (FR-17).

    history — предыдущие реплики треда, query — текущий вопрос (оба уже
    деперсонализированы scrub_for_llm), chunks — найденные фрагменты КБ
    [{"text", "source_title"}]. patient_contexts — карты пациентов треда,
    ОБЯЗАТЕЛЬНО уже маскированные (services.assistant_chat._scrub_patient_contexts):
    в промпт идёт только обезличенная карта, ФИО и patient_id не переносятся.
    Карта не заменяет сниппеты: запрос «только по карте» (без находок в КБ)
    до модели не доходит и отвечается детерминированным build_rag_answer.
    Второй элемент пары — причина для UI (`answer_kind`/`llm_reason` в
    services.agent): ok — текст от модели; disabled — LLM выключен/нет
    сниппетов; timeout — не уложились в assistant_llm_answer_timeout (включая
    connect-timeout шлюза); http_error — шлюз ответил ошибкой/мусором;
    truncated — апстрим сам оборвал генерацию (finish_reason=length — бюджет
    шлюза/модели, или EOF без терминального чанка), текст неполный;
    empty — модель вернула пустой текст, в том числе finish_reason=length с
    пустым content (reasoning-модель съела весь лимит генерации на
    «размышления»). Проверку
    ссылок [n] и сборку citations делает вызывающий: здесь только текст;
    границу номеров даёт answer_snippet_window(chunks).
    """
    body = _answer_body(history, query, chunks, patient_contexts)
    if body is None:
        return None, "disabled"
    try:
        response = _request(body, timeout=timeout or settings.assistant_llm_answer_timeout)
    except TimeoutError as exc:
        logger.warning(
            "assistant LLM-ответ недоступен (TimeoutError: %s) — детерминированный ответ",
            exc,
        )
        return None, "timeout"
    except urllib.error.URLError as exc:
        # connect-timeout urllib заворачивает в URLError(reason=TimeoutError),
        # read-timeout приходит голым TimeoutError (ветка выше): для UI это
        # одна причина — "timeout", а не http_error.
        reason = stream_error_reason(exc)
        logger.warning(
            "assistant LLM-ответ недоступен (URLError: %s) — детерминированный ответ",
            exc.reason,
        )
        return None, reason
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning(
            "assistant LLM-ответ недоступен (%s: %s) — детерминированный ответ",
            type(exc).__name__,
            exc,
        )
        return None, "http_error"
    try:
        choice = response["choices"][0]
        content = choice["message"].get("content") or ""
        finish_reason = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        logger.warning(
            "assistant LLM вернул неожиданный формат ответа (%s) — детерминированный ответ",
            type(exc).__name__,
        )
        return None, "http_error"
    answer = clean_answer(content)
    if answer is None:
        logger.warning(
            "assistant LLM вернул пустой ответ (finish_reason=%s) — детерминированный ответ",
            finish_reason,
        )
        return None, "empty"
    if finish_reason == "length":
        # Усечённый клинический текст нельзя отдавать как ответ ИИ: обрыв на
        # полуслове врёт о полноте рекомендации. Показываем фрагменты.
        logger.warning(
            "assistant LLM оборван апстримом (finish_reason=length, %d симв.)"
            " — детерминированный ответ",
            len(answer),
        )
        return None, "truncated"
    return answer, "ok"


def compose_answer(
    history: list[dict],
    query: str,
    chunks: list[dict],
    *,
    timeout: float | None = None,
    patient_contexts: list[dict] | None = None,
) -> str | None:
    """Текст ответа модели или None (обёртка над compose_answer_result)."""
    return compose_answer_result(
        history, query, chunks, timeout=timeout, patient_contexts=patient_contexts
    )[0]


def stream_error_reason(exc: BaseException) -> str:
    """Причина отказа LLM — тот же словарь, что у compose_answer_result.

    timeout|truncated|http_error. urllib прячет таймауты в
    `URLError.reason` (connect-timeout) — разворачиваем вложенную причину,
    иначе в логах и UI такой отказ выглядел бы как ошибка шлюза.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, TruncatedAnswer):
            return "truncated"
        if isinstance(cur, (TimeoutError, httpx.TimeoutException)):
            return "timeout"
        nested = cur.reason if isinstance(cur, urllib.error.URLError) else None
        cur = nested if isinstance(nested, BaseException) else None
    return "http_error"


async def compose_answer_stream(
    history: list[dict],
    query: str,
    chunks: list[dict],
    *,
    timeout: float | None = None,
    patient_contexts: list[dict] | None = None,
) -> AsyncIterator[str]:
    """Токены ответа по сниппетам из OpenAI-совместимого `stream: true`.

    Отдаёт только `delta.content`; пустые служебные чанки пропускает. Ошибка
    транспорта или невалидный SSE пробрасываются вызывающему: `services.agent`
    тогда отдаёт детерминированный build_rag_answer целиком.

    patient_contexts — те же маскированные карты, что у batch-ответа: тело
    запроса собирает общий _answer_body, поэтому поток видит ровно тот же блок.

    Обрыв потока без терминального чанка (`finish_reason`/`[DONE]`) — это не
    завершённый ответ: шлюз или прокси мог закрыть соединение посреди
    генерации, и частичный текст нельзя отдавать как ответ ИИ. Такой EOF
    поднимает `TruncatedAnswer`, как и `finish_reason=length` от апстрима.
    """
    finished = False
    body = _answer_body(history, query, chunks, patient_contexts)
    if body is None:
        return
    body["stream"] = True
    url = settings.assistant_llm_url.rstrip("/") + "/chat/completions"
    # httpx read-timeout сбрасывается на каждом чанке, а шлюз может слать
    # keepalive без контента. Дедлайн — на весь поток, как у batch-ответа.
    budget = timeout or settings.assistant_llm_answer_timeout
    with anyio.move_on_after(budget) as scope:
        async with httpx.AsyncClient(timeout=budget) as client:
            async with client.stream("POST", url, json=body, headers=_llm_headers()) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        finished = True
                        break
                    payload = json.loads(data)
                    # Финальный usage-чанк приходит с choices: [] — не ошибка.
                    choices = payload.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        yield piece
                    # Не ждём TCP-закрытия/шлюзового [DONE]: модель уже закончила.
                    finish_reason = choice.get("finish_reason")
                    if finish_reason == "length":
                        # Генерацию оборвал апстрим (его собственный лимит):
                        # обрыв отдавать как ответ ИИ нельзя — вызывающий
                        # уйдёт на фрагменты.
                        raise TruncatedAnswer("LLM stream hit finish_reason=length")
                    if finish_reason:
                        finished = True
                        break
    if scope.cancelled_caught:
        raise TimeoutError("LLM stream timed out")
    if not finished:
        raise TruncatedAnswer("LLM stream ended without finish_reason/[DONE]")
