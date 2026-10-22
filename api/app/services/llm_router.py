"""LLM-роутер раздела БЗ для ассистента врача (docs/04 FR-14, docs/08).

Вместо ручного выбора «Раздела» в UI (удалён) топик выбирает LLM: запрос
отправляется на OpenAI-совместимый /v1/chat/completions с набором тулз
search_<ТОПИК>, модель обязана вернуть ровно один tool_call, из имени тулзы
выводится topic. Ответ по-прежнему строит детерминированный build_rag_answer —
LLM только роутит поиск, не отвечает клинически (красные линии docs/01).

Границы (docs/12): на вход LLM подаётся только очищенный от ПДн запрос
(scrub_for_llm вызывается в routers/assistant.py до invoke). Контекст пациента
в LLM НЕ уходит.

Отказоустойчивость: URL не задан / таймаут / ошибка HTTP / пустой tool_call →
возвращаем None = поиск по всем разделам (текущее поведение без LLM).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from app.config import settings

logger = logging.getLogger(__name__)

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


def route_topic(query: str) -> str | None:
    """Выбирает topic (ШКАЛЫ/ТРИАЖ/РЕКОМЕНДАЦИИ/ЛЕКАРСТВА) через LLM.

    None = поиск по всем разделам (LLM выключен/ошибка/вопрос вне разделов).
    """
    if not settings.assistant_llm_url:
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
            # llama.cpp-сервер понимает этот флаг; прочие шлюзы его игнорируют.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        data = json.dumps(payload).encode("utf-8")
        url = settings.assistant_llm_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        api_key = settings.assistant_llm_api_key.strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=settings.assistant_llm_timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
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
