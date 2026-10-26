"""FR-44 (docs/18 §10, PR P9): протокол как тул агента и диалог по сессии.

Три входа в один конвейер «контекст пациента → расшифровка → протокол»:

- `draft_session_protocol` — тул агента: черновик создаётся существующим
  `session_ai.draft()`, а модели возвращается только компактная сводка (без
  текста протокола). `session_id` из аргументов модели НЕ даёт доступа к чужим
  сессиям: тул всегда работает с сессией, которую открыл врач (bound session).
- `ask()` — `POST …/sessions/{id}/chat`: tool-loop (максимум 2 раунда вызовов,
  `tool_choice: auto`) с тулами `search_kb` и `draft_session_protocol`.
- `history()` — `GET …/sessions/{id}/chat`: реплики диалога.

Инварианты:
- текст ответа НЕ пишет модель от себя: он собирается детерминированной
  `agent.compose_grounded_answer` по найденным чанкам (проверка ссылок `[n]`),
  а при её отказе — `build_rag_answer` (docs/07 §5, FR-17);
- чат НЕ мутирует протокол: правку раздела агент возвращает как `proposal`,
  применяет врач явным `PUT …/protocol` (кнопка «Применить» в UI). Тул черновика
  — единственная мутация, и она штатная (создание/обновление черновика);
- ПДн: в LLM уходит только замаскированное — текст расшифровки замаскирован при
  расшифровке, карточка пациента и реплики проходят `scrub_for_llm`/`scrub_text`
  на каждом вызове; в БД реплики только шифротекстом; в аудите только id;
- чат не блокируется завершением приёма (409 `SESSION_FINISHED` касается записи и
  расшифровки): разбор приёма и проверка протокола после `finish` — штатный
  сценарий, документ при этом остаётся неизменным до явной правки врачом.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

import anyio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from compass.agent import RAGChunk, build_rag_answer

from app.config import settings
from app.errors import Problem
from app.models import ClinicalSession, SessionChatMessage

from . import agent as doctor_agent
from . import audit, llm_router, protocols, session_ai
from .agent import scrub_for_llm, shared_factory
from .clinician import patient_context
from .depersonalize import deny_names, scrub_text
from .sessions import require_patient_access, require_session

log = logging.getLogger(__name__)

DRAFT_TOOL = "draft_session_protocol"
SEARCH_TOOL = "search_kb"
# Два раунда вызовов тулз — потолок FR-44: больше в диалоге по приёму не нужно,
# а каждый раунд — отдельный вызов модели.
MAX_TOOL_ROUNDS = 2

ROLE_CLINICIAN = "clinician"
ROLE_ASSISTANT = "assistant"

# Разделы, правку которых агент МОЖЕТ предложить. `mental_status` — объект из
# полей, поэтому предлагаются его поля отдельными ключами: правка «одним куском»
# сломала бы структуру §5.1. Лечение и МКБ не предлагаются вовсе: там нужны
# источники по каждому элементу (§5.3), а не свободный текст модели.
PROPOSAL_SECTIONS = (
    "complaints",
    "anamnesis_morbi",
    "somatic_status",
    "recommendations",
    "diagnosis.rationale",
    "mental_status.appearance_behavior",
    "mental_status.consciousness_orientation",
    "mental_status.perception",
    "mental_status.thinking_speech",
    "mental_status.emotions_volition",
    "mental_status.criticism",
    "mental_status.suicidal_risk",
)

_FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)

_SYSTEM_PROMPT = """Ты — ассистент врача-психиатра внутри одного приёма.
У тебя есть данные приёма: обезличенная карточка пациента, текущий черновик
протокола и последние реплики диалога с врачом.

Правила:
1. Клинических утверждений от себя не пиши: всё, что утверждается, должно
   опираться на найденные фрагменты базы знаний (тул search_kb) или на разделы
   черновика протокола. Текст ответа врач увидит отдельно — его собирает сервер
   из найденных фрагментов, поэтому твоя задача — выбрать, что искать, и
   предложить правку раздела.
2. Протокол ты НЕ меняешь. Если врач просит поменять раздел, верни
   `proposal` = {section, after, reason, sources}, где section — ровно один из
   разрешённых ключей, after — новая редакция раздела целиком, reason —
   короткое обоснование, sources — номера найденных фрагментов ([1], [2], …),
   на которые опирается правка. Применяет правку врач сам.
3. Ответ — ОДИН JSON-объект: {"answer": "<краткий комментарий врачу>",
   "proposal": null | {...}}. Без markdown-обёрток и текста вокруг.
4. Не пересказывай и не выдумывай персональные данные: всё уже обезличено.
"""

_TOOL_SEARCH = {
    "type": "function",
    "function": {
        "name": SEARCH_TOOL,
        "description": (
            "Гибридный поиск по базе знаний (клинические рекомендации, шкалы, "
            "препараты). Возвращает нумерованные фрагменты с цитатами."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос"},
                "topic": {
                    "type": "string",
                    "description": "Необязательный раздел БЗ: ЛЕКАРСТВА, РЕКОМЕНДАЦИИ, ШКАЛЫ, ТРИАЖ",
                },
            },
            "required": ["query"],
        },
    },
}

_TOOL_DRAFT = {
    "type": "function",
    "function": {
        "name": DRAFT_TOOL,
        "description": (
            "Черновик протокола по расшифровке приёма. Возвращает сводку без "
            "текста протокола. Доступна только текущая сессия."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "id текущей сессии приёма (та, что открыта у врача)",
                }
            },
            "required": ["session_id"],
        },
    },
}

_TOOLS = [_TOOL_SEARCH, _TOOL_DRAFT]


def _ensure_enabled() -> None:
    if not settings.session_ai_enabled:
        raise Problem(
            503, "SESSION_AI_DISABLED", "ИИ-ассистент сессии выключен (CC_SESSION_AI_ENABLED)"
        )


def _ensure_llm() -> None:
    if not settings.assistant_llm_url.strip():
        raise Problem(
            503, "MODEL_NOT_CONFIGURED", "LLM не настроен (CC_ASSISTANT_LLM_URL)"
        )


# --- тул черновика -----------------------------------------------------------


def _filled(sections: dict) -> list[str]:
    """Разделы с содержимым — только имена, без текста (контекст модели)."""
    filled: list[str] = []
    for name in ("complaints", "anamnesis_morbi", "mental_status", "somatic_status", "diagnosis", "treatment", "recommendations"):
        value = sections.get(name)
        if isinstance(value, str):
            if value.strip():
                filled.append(name)
        elif isinstance(value, dict):
            if any(str(v).strip() for v in value.values() if isinstance(v, str)):
                filled.append(name)
        elif isinstance(value, list) and value:
            filled.append(name)
    return filled


def _compact(protocol: dict) -> dict:
    """Сводка для модели: ни текста разделов, ни цитат — только счётчики."""
    sections = protocol.get("sections") or {}
    return {
        "protocol_id": protocol.get("id"),
        "version": protocol.get("version"),
        "filled_sections": _filled(sections),
        "needs_review_count": len(protocol.get("needs_review") or []),
        "sources_count": len(sections.get("sources") or []),
    }


async def draft_session_protocol(
    db: AsyncSession,
    clinician_id,
    session_id,
    *,
    bound_session_id,
    ip: str | None = None,
    action: str = "SESSION_PROTOCOL_CHAT_DRAFT",
) -> dict:
    """Тул агента: черновик протокола текущей сессии + компактная сводка.

    `session_id` пришёл от модели, поэтому доверять ему нельзя: он обязан
    совпасть с сессией, которую открыл врач (`bound_session_id`), иначе — 404
    (чужой id не отличается от несуществующего: id не подтверждаем). Сама
    работа — существующий `session_ai.draft()` с его же проверками
    (роутер/согласие/409 PROTOCOL_LOCKED на утверждённом протоколе).
    """
    # Аргумент модели — строка; сравнение только по нормализованному UUID, иначе
    # «тот же» id в другом регистре/с пробелом выглядел бы чужим.
    try:
        requested = uuid.UUID(str(session_id).strip())
        bound = (
            bound_session_id
            if isinstance(bound_session_id, uuid.UUID)
            else uuid.UUID(str(bound_session_id).strip())
        )
    except (ValueError, AttributeError, TypeError):
        raise Problem(404, "SESSION_NOT_FOUND", "Сессия не найдена") from None
    if requested != bound:
        log.warning(
            "Тул черновика: модель запросила чужую сессию %s при открытой %s — отказ",
            requested,
            bound,
        )
        raise Problem(404, "SESSION_NOT_FOUND", "Сессия не найдена")
    protocol = await session_ai.draft(db, clinician_id, bound, ip=ip, action=action)
    return _compact(protocol)


# --- диалог ------------------------------------------------------------------


def _section_text(sections: dict | None, key: str) -> str:
    """Текущее значение предлагаемого раздела (для `before` в proposal)."""
    if not sections:
        return ""
    if "." in key:
        head, field = key.split(".", 1)
        value = sections.get(head)
        if isinstance(value, dict):
            return str(value.get(field) or "")
        return ""
    value = sections.get(key)
    return value if isinstance(value, str) else ""


def _json_object(text: str) -> dict | None:
    match = _FENCE.search(text or "")
    body = (match.group(1) if match else text or "").strip()
    if not body:
        return None
    try:
        data = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _citations(answer) -> list[dict]:
    return [
        {
            "source_title": c.source_title,
            "guideline_id": c.guideline_id,
            "topic": c.topic,
            "score": c.score,
        }
        for c in answer.citations
    ]


def _proposal(raw, sections: dict | None, chunks: list[RAGChunk]) -> dict | None:
    """Предложение правки раздела: без записи в БД (применяет врач).

    `before` берётся из текущего черновика, а не из ответа модели: модель могла
    не видеть последнюю редакцию, а UI показывает diff именно к сохранённой.
    Неподходящий раздел/пустая правка/правка без изменений — не предложение.
    """
    if not isinstance(raw, dict) or sections is None:
        return None
    section = str(raw.get("section") or "").strip()
    if section not in PROPOSAL_SECTIONS:
        return None
    after = str(raw.get("after") or "").strip()
    before = _section_text(sections, section)
    if not after or after == before.strip():
        return None
    numbers: list[int] = []
    for value in raw.get("sources") or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= len(chunks) and number not in numbers:
            numbers.append(number)
    return {
        "section": section,
        "before": before,
        "after": after,
        "reason": str(raw.get("reason") or "")[:500],
        "sources": [
            {
                "ref": number,
                "source_title": chunks[number - 1].source_title,
                "guideline_id": chunks[number - 1].guideline_id,
                "topic": chunks[number - 1].topic,
            }
            for number in numbers
        ],
    }


def _message(row: SessionChatMessage) -> dict:
    """Реплика → контракт ручки. Текст расшифровывается в процессе, не в ответе."""
    raw = row.content_cipher or ""
    data: dict = {}
    if row.role == ROLE_ASSISTANT:
        parsed = _json_object(raw) if raw.lstrip().startswith("{") else None
        data = parsed or {"answer": raw}
    else:
        data = {"answer": raw}
    return {
        "id": str(row.id),
        "role": row.role,
        "content": str(data.get("answer") or ""),
        "citations": list(data.get("citations") or []),
        "proposal": data.get("proposal"),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _mask(text: str, names: list[str]) -> str:
    return scrub_text(text, names=names).text


def _snippet_payload(chunks: list[RAGChunk], offset: int) -> list[dict]:
    return [
        {"n": offset + i, "title": c.source_title, "text": c.text[:600]}
        for i, c in enumerate(chunks, start=1)
    ]


def _merge(chunks: list[RAGChunk], found: list[RAGChunk]) -> list[RAGChunk]:
    """Дописать новые фрагменты, не теряя нумерацию уже показанных модели."""
    seen = {c.chunk_id or c.text for c in chunks}
    for chunk in found:
        key = chunk.chunk_id or chunk.text
        if key in seen:
            continue
        seen.add(key)
        chunks.append(chunk)
    return chunks


async def _search(query: str, topic: str | None, limit: int) -> list[RAGChunk]:
    """`search_kb` в рабочем потоке: Qdrant-клиент синхронный.

    Red line остаётся включённой (в отличие от черновика протокола §5.3): чат
    не подсказывает терапию/диагноз, отказ формирует `build_rag_answer`.
    """
    factory = shared_factory()
    return await anyio.to_thread.run_sync(
        lambda: factory.search_kb(query, topic=topic or None, limit=limit)
    )


async def _run_tool(
    db: AsyncSession,
    clinician_id,
    session: ClinicalSession,
    call: dict,
    chunks: list[RAGChunk],
    *,
    ip: str | None,
) -> dict:
    """Один вызов тула из tool-loop. Ошибки — в результат, а не в исключение.

    Модель должна увидеть отказ (`error`) и продолжить: сбой поиска или 409
    PROTOCOL_LOCKED — не повод ронять ответ врачу.
    """
    fn = (call.get("function") or {}).get("name") or ""
    raw_args = (call.get("function") or {}).get("arguments") or "{}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    except (ValueError, json.JSONDecodeError):
        args = {}
    if not isinstance(args, dict):
        args = {}

    if fn == SEARCH_TOOL:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "EMPTY_QUERY"}
        topic = str(args.get("topic") or "").strip() or None
        try:
            found = await _search(query, topic, settings.session_chat_sources)
        except Exception as exc:  # noqa: BLE001 — KB недоступна: ответ без неё
            log.warning("Диалог сессии %s: поиск KB недоступен (%s)", session.id, type(exc).__name__)
            return {"error": "KB_UNAVAILABLE", "snippets": []}
        offset = len(chunks)
        _merge(chunks, found)
        return {"snippets": _snippet_payload(chunks[offset:], offset)}

    if fn == DRAFT_TOOL:
        try:
            summary = await draft_session_protocol(
                db,
                clinician_id,
                args.get("session_id") or "",
                bound_session_id=session.id,
                ip=ip,
            )
        except Problem as exc:
            return {"error": exc.code}
        except Exception as exc:  # noqa: BLE001 — сбой тула не роняет диалог (как у search_kb)
            log.warning(
                "Диалог сессии %s: тул черновика недоступен (%s)",
                session.id,
                type(exc).__name__,
            )
            return {"error": "DRAFT_UNAVAILABLE"}
        return summary

    return {"error": "UNKNOWN_TOOL"}


def _context_block(
    session: ClinicalSession,
    card: dict | None,
    sections: dict | None,
    chunks: list[RAGChunk],
    question: str,
) -> str:
    parts = [
        f"Приём: канал {session.channel}, статус {'завершён' if session.status == 'finished' else 'активен'}.",
        "Карточка пациента (обезличена): "
        + json.dumps(card or {}, ensure_ascii=False)[:4000],
    ]
    if sections is None:
        parts.append("Черновик протокола ещё не создан.")
    else:
        parts.append(
            "Черновик протокола (обезличен): "
            + json.dumps(sections, ensure_ascii=False)[:6000]
        )
    parts.append("Разрешённые разделы для proposal: " + ", ".join(PROPOSAL_SECTIONS))
    if chunks:
        parts.append(
            "Фрагменты базы знаний:\n"
            + "\n".join(
                f"[{i}] {c.source_title}: {c.text[:500]}"
                for i, c in enumerate(chunks, start=1)
            )
        )
    else:
        parts.append("Фрагментов базы знаний пока нет.")
    parts.append(f"Вопрос врача: {question}")
    return "\n\n".join(parts)


async def _loop(
    db: AsyncSession,
    clinician_id,
    session: ClinicalSession,
    messages: list[dict],
    chunks: list[RAGChunk],
    *,
    ip: str | None,
) -> dict | None:
    """Tool-loop (≤ MAX_TOOL_ROUNDS раундов) → JSON-объект ответа модели."""
    content = ""
    for round_no in range(MAX_TOOL_ROUNDS + 1):
        tools = _TOOLS if round_no < MAX_TOOL_ROUNDS else None
        try:
            body = await anyio.to_thread.run_sync(
                lambda t=tools: llm_router.chat_with_tools(
                    messages, t, timeout=settings.session_ai_timeout
                )
            )
        except Exception as exc:  # noqa: BLE001 — модель недоступна: детерминированный ответ
            log.warning(
                "Диалог сессии %s: LLM недоступен (%s)", session.id, type(exc).__name__
            )
            return None
        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = str(msg.get("content") or "")
        calls = msg.get("tool_calls") or []
        if not calls:
            return _json_object(content)
        messages.append(
            {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        )
        for call in calls:
            result = await _run_tool(db, clinician_id, session, call, chunks, ip=ip)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "name": (call.get("function") or {}).get("name") or "",
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
    return _json_object(content)


async def ask(
    db: AsyncSession,
    clinician_id,
    session_id,
    message: str,
    *,
    ip: str | None = None,
) -> dict:
    """`POST …/sessions/{id}/chat`: вопрос врачу-агенту по текущему приёму."""
    _ensure_enabled()
    _ensure_llm()
    session = await require_session(db, clinician_id, session_id)
    # 403 NO_ROSTER / CONSENT_REQUIRED — карта пациента без ростра и согласия
    # недоступна (152-ФЗ). Аудит ASK_ASSISTANT_WITH_PATIENT пишет patient_context.
    await require_patient_access(db, clinician_id, session.patient_id)

    text = (message or "").strip()
    if not text:
        raise Problem(422, "MESSAGE_EMPTY", "Сообщение пустое")
    limit = settings.session_chat_message_chars
    if len(text) > limit:
        raise Problem(422, "MESSAGE_TOO_LONG", f"Сообщение длиннее {limit} символов")

    asked = await db.scalar(
        select(func.count())
        .select_from(SessionChatMessage)
        .where(
            SessionChatMessage.session_id == session.id,
            SessionChatMessage.role == ROLE_CLINICIAN,
        )
    )
    if (asked or 0) >= settings.session_chat_max_turns:
        raise Problem(
            409, "CHAT_LIMIT_REACHED", "Лимит реплик по этой сессии исчерпан"
        )

    card = await patient_context(db, clinician_id, session.patient_id, ip=ip)
    names = await deny_names(db, session.clinician_id, session.patient_id, session.clinician_id)

    masked_text, masked_card, _ = await anyio.to_thread.run_sync(
        lambda: scrub_for_llm(text, card, names)
    )

    row = await protocols.active(db, session.id)
    sections = protocols.decode_sections(row) if row is not None and row.status == "draft" else None
    if sections is not None:
        # Черновик собран из уже замаскированной расшифровки, но свободный текст
        # мог быть дописан врачом — прогоняем через тот же scrub перед egress.
        masked_json = await anyio.to_thread.run_sync(
            lambda: scrub_text(json.dumps(sections, ensure_ascii=False), names=names).text
        )
        sections_block = json.loads(masked_json) if masked_json.lstrip().startswith("{") else sections
    else:
        sections_block = None

    history_rows = await _history_rows(db, session.id, settings.session_chat_history)
    history = [
        {
            "role": "user" if item.role == ROLE_CLINICIAN else "assistant",
            "content": _mask(
                _message(item)["content"], names
            ),
        }
        for item in history_rows
    ]

    # Время реплик — клиентское, не `now()` сервера: обе строки одного хода
    # пишутся в одной транзакции, а `now()` у них совпал бы до микросекунды, и
    # порядок истории («вопрос, затем ответ») стал бы случайным.
    asked_at = _now()
    db.add(
        SessionChatMessage(
            session_id=session.id,
            clinician_id=clinician_id,
            role=ROLE_CLINICIAN,
            content_cipher=text,
            created_at=asked_at,
        )
    )
    await db.flush()

    # Детерминированный ретрив до модели: даже без вызова тула у сервера есть
    # фрагменты, по которым собирается ответ (или честный отказ).
    chunks: list[RAGChunk] = []
    try:
        _merge(chunks, await _search(masked_text, None, settings.session_chat_sources))
    except Exception as exc:  # noqa: BLE001 — KB недоступна: ответ по карточке
        log.warning("Диалог сессии %s: поиск KB недоступен (%s)", session.id, type(exc).__name__)

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        *history,
        {
            "role": "user",
            "content": _context_block(session, masked_card, sections_block, chunks, masked_text),
        },
    ]
    data = await _loop(db, clinician_id, session, messages, chunks, ip=ip) or {}

    answer = await anyio.to_thread.run_sync(
        lambda: doctor_agent.compose_grounded_answer(masked_text, history, chunks)
    )
    if answer is None:
        answer = build_rag_answer(masked_text, chunks, patient_context=masked_card)

    proposal = _proposal(data.get("proposal"), sections, chunks)
    payload = {
        "answer": answer.answer,
        "citations": _citations(answer),
        "proposal": proposal,
    }
    reply = SessionChatMessage(
        session_id=session.id,
        clinician_id=clinician_id,
        role=ROLE_ASSISTANT,
        content_cipher=json.dumps(payload, ensure_ascii=False),
        created_at=max(_now(), asked_at + timedelta(microseconds=1)),
    )
    db.add(reply)
    await audit.record(
        db, clinician_id, "SESSION_CHAT_ASK", "clinical_session", str(session.id), ip=ip
    )
    await db.commit()
    await db.refresh(reply)
    return _message(reply)



def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _history_rows(db: AsyncSession, session_id, limit: int) -> list[SessionChatMessage]:
    if limit <= 0:
        return []
    rows = (
        await db.scalars(
            select(SessionChatMessage)
            .where(SessionChatMessage.session_id == session_id)
            .order_by(SessionChatMessage.created_at.desc(), SessionChatMessage.id.desc())
            .limit(limit)
        )
    ).all()
    return list(reversed(rows))


async def history(db: AsyncSession, clinician_id, session_id, *, limit: int = 50) -> dict:
    """`GET …/sessions/{id}/chat`: **последние** `limit` реплик (старые → новые).

    Лимит применяется к убывающему порядку: иначе на длинном диалоге панель
    показывала бы самые старые реплики, а свежие пропадали бы после перезагрузки.
    """
    session = await require_session(db, clinician_id, session_id)
    await require_patient_access(db, clinician_id, session.patient_id)
    rows = await _history_rows(db, session.id, limit)
    return {"items": [_message(row) for row in rows]}
