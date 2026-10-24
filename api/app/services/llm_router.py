"""LLM-роутер раздела БЗ для ассистента врача (docs/04 FR-14, docs/08).

Вместо ручного выбора «Раздела» в UI (удалён) топик выбирает LLM: запрос
отправляется на OpenAI-совместимый /v1/chat/completions с набором тулз
search_<ТОПИК>, модель обязана вернуть ровно один tool_call, из имени тулзы
выводится topic. Ответ по-прежнему строит детерминированный build_rag_answer —
LLM только роутит поиск, не отвечает клинически (красные линии docs/01).

Два вызова LLM, оба — только маршрутизация поиска, не клинический ответ:
route_topic (FR-14) выбирает раздел БЗ, rewrite_followup (FR-21) разворачивает
уточняющий вопрос треда («а в каких дозах?») в самодостаточный поисковый запрос.

Границы (docs/12): на вход LLM подаётся только очищенный от ПДн текст
(scrub_for_llm вызывается в routers/assistant.py и assistant_chat.py до вызова).
Контекст пациента в LLM НЕ уходит.

Отказоустойчивость: URL не задан / таймаут / ошибка HTTP / пустой ответ →
route_topic возвращает None (= поиск по всем разделам), rewrite_followup —
None (= поиск по исходному вопросу). Ответ не ломается.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

from app.config import settings

logger = logging.getLogger(__name__)

# Переформулировка уточняющего вопроса (FR-21): границы истории, уходящей в LLM.
REWRITE_HISTORY_MESSAGES = 8      # последних сообщений треда
REWRITE_MESSAGE_CHARS = 800       # обрезка одного сообщения
REWRITE_HISTORY_CHARS = 3000      # обрезка всей истории (с конца, свежее важнее)
REWRITE_QUERY_CHARS = 800         # обрезка текущего вопроса и переформулировки

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
    "Определи, к какому разделу относится вопрос пользователя, и вызови ровно одну "
    "тулзу поиска. Если вопрос не относится ни к одному разделу — не вызывай тулзу. "
    "Никогда не отвечай текстом от себя: только вызов тулзы (или ничего)."
)


def _enabled() -> bool:
    return bool(settings.assistant_llm_url.strip())


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
    """Сырой POST /chat/completions для других LLM-задач (FR-31, docs/17).

    Транспорт общий с роутером топиков: таймаут/ключ/URL берутся из settings.
    Ошибки транспорта и невалидный JSON не ловим — их обрабатывает вызывающий.
    """
    return _request(
        {
            "model": settings.assistant_llm_model,
            "messages": messages,
            "temperature": temperature,
        },
        timeout=timeout,
    )


def route_topic(query: str) -> str | None:
    """Выбирает topic (ШКАЛЫ/ТРИАЖ/РЕКОМЕНДАЦИИ/ЛЕКАРСТВА) через LLM.

    None = поиск по всем разделам (LLM выключен/ошибка/вопрос вне разделов).
    """
    if not _enabled():
        return None
    if not query.strip():
        return None
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
            # chat_template_kwargs: llama.cpp понимает флаг, прочие шлюзы игнорируют.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        body = _request(payload)
        msg = body["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return None  # вне разделов — поиск по всем
        fn = tool_calls[0].get("function", {}).get("name", "")
        topic = TOPIC_TOOLS.get(fn)
        if topic is None:
            logger.warning("assistant LLM вернул неизвестную тулзу %r — поиск по всем", fn)
            return None
        return topic
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("assistant LLM-роутинг недоступен (%s: %s) — поиск по всем", type(exc).__name__, exc)
        return None


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
        body = _request(
            {
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
                "chat_template_kwargs": {"enable_thinking": False},
            }
        )
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
