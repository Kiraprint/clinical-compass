"""Сессии клинического ассистента врача (FR-21).

Тред хранит контекст диалога между запросами и область пациентов (мультипациент).
Ответ собирает RAG-путь (services.agent): при настроенном `CC_ASSISTANT_LLM_URL`
его пишет LLM по найденным сниппетам и истории треда, иначе (и при любом сбое
модели) — детерминированный `build_rag_answer`. Уточняющий вопрос («а в каких
дозах?») ищется по LLM-переформулировке последних реплик треда. История в
embedding НЕ конкатенируется (ФИО/redline-реплики из истории сдвигали поиск), а
в LLM уходит только после scrub_for_llm: и для переформулировки, и для ответа.
Red line, отказ и цитаты считаются по исходному вопросу (FR-21), карты
пациентов в промпт не попадают.

ПДн: текст сообщений — спецкатегория (EncryptedText), карты пациентов попадают
в контекст только через clinician.patient_context (roster-gate + согласие).
Обращение к треду с пациентами аудируется READ_ASSISTANT_THREAD.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator

import anyio

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from compass.agent import detect_redline

from app.config import settings
from app.enrichment import review as kb_review
from app.errors import Problem
from app.kb_seed import load_drug_docs
from app.models import (
    AssistantMessage,
    AssistantThread,
    AssistantThreadPatient,
    ClinicianRoster,
)
from app.services import audit, clinician, llm_router
from app.services.agent import scrub_for_llm, shared_factory
from app.services.depersonalize import deny_names as deny_names_for_llm

MAX_THREAD_PATIENTS = 10
# Потолок одновременных прогонов агента на процесс. RAG блокирующий и дорогой
# (Qdrant + эмбеддинги + LLM), поэтому у него собственный limiter, а не общий
# пул AnyIO (40 потоков): всплеск запросов к ассистенту ждёт здесь и не съедает
# токены пула у push/эмбеддингов/Qdrant других ручек (docs/03 §12).
AGENT_LIMITER = anyio.CapacityLimiter(settings.assistant_thread_limit)

def _not_found() -> Problem:
    return Problem(404, "THREAD_NOT_FOUND", "Диалог не найден")

def _timeout() -> Problem:
    """Дедлайн прогона агента. Retry-After — клиенту есть смысл повторить."""
    return Problem(
        503,
        "ASSISTANT_TIMEOUT",
        f"Ассистент не ответил за {settings.assistant_deadline_s:g} с, повторите запрос",
        headers={"Retry-After": "5"},
    )

async def _invoke_agent(query: str, **kwargs) -> dict:
    """RAG-граф (Qdrant + эмбеддинги + LLM-роутинг) — в поток: вызов блокирующий.

    Сессия БД к этому моменту закоммичена, поэтому соединение не удерживается,
    пока агент ходит во внешние сервисы. Дедлайн (`CC_ASSISTANT_DEADLINE_S`)
    покрывает и ожидание лимитера, и сам прогон: `move_on_after`, а не
    `fail_after`, — собственный TimeoutError агента (например, таймаут
    LLM-роутера) не должен подменяться нашим дедлайном.

    `abandon_on_cancel=True` обязателен: иначе `run_sync` (дефолт
    `abandon_on_cancel=False`) дожидается потока и дедлайн не срабатывает.
    Брошенный поток досчитывает сам и держит токен `AGENT_LIMITER` до конца —
    поэтому параллелизм агента и ограничен этим лимитером.
    """
    factory = shared_factory()
    result: dict | None = None
    # budget — тот же дедлайн, что ограничивает прогон: agent берёт его для
    # таймаутов LLM (роутер и ответ идут последовательно внутри прогона).
    # Считаем ДО захвата лимитера: иначе время в очереди не входит в бюджет и
    # прогон упирается во внешний move_on_after → 503 вместо фрагментов.
    deadline_at = time.monotonic() + settings.assistant_deadline_s
    with anyio.move_on_after(settings.assistant_deadline_s) as scope:
        result = await anyio.to_thread.run_sync(
            lambda: factory.invoke(query, deadline_at=deadline_at, **kwargs),
            abandon_on_cancel=True,
            limiter=AGENT_LIMITER,
        )
    if scope.cancelled_caught:
        raise _timeout()
    return result


@dataclass
class _Turn:
    """Всё, что подготовлено до вызова агента и нужно после него."""

    thread: AssistantThread
    ids: list[uuid.UUID]
    history: list[dict]
    question: AssistantMessage
    skipped: list[str]
    exclude_ids: set[str]
    allowed_work_ids: set[str] | None
    corpus_drugs: list[dict]
    scrubbed_query: str
    scrubbed_contexts: list[dict]
    search_query: str
    rewritten: bool


async def _stream_agent(query: str, **kwargs) -> AsyncIterator[dict]:
    """То же, что _invoke_agent, но токенами; лимитер держится весь поток."""
    factory = shared_factory()
    budget = time.monotonic() + settings.assistant_deadline_s
    with anyio.move_on_after(settings.assistant_deadline_s) as scope:
        async with AGENT_LIMITER:
            async for item in factory.stream(query, deadline_at=budget, **kwargs):
                yield item
    if scope.cancelled_caught:
        raise _timeout()

async def normalize_patient_ids(db: AsyncSession, clinician_id, patient_ids) -> list[uuid.UUID]:
    """Дедуп + лимит + roster-гейт. Возвращает id в порядке перечисления."""
    uniq: list[uuid.UUID] = []
    for pid in patient_ids or []:
        pid = uuid.UUID(str(pid))
        if pid not in uniq:
            uniq.append(pid)
    if len(uniq) > MAX_THREAD_PATIENTS:
        raise Problem(
            422,
            "TOO_MANY_PATIENTS",
            f"В диалоге не может быть больше {MAX_THREAD_PATIENTS} пациентов",
        )
    if uniq:
        roster = set(
            await db.scalars(
                select(ClinicianRoster.patient_id).where(
                    ClinicianRoster.clinician_id == clinician_id,
                    ClinicianRoster.patient_id.in_(uniq),
                )
            )
        )
        missing = [str(p) for p in uniq if p not in roster]
        if missing:
            raise Problem(
                403, "PATIENT_NOT_IN_ROSTER", "Пациент не закреплён за этим врачом"
            )
    return uniq

async def get_thread(db: AsyncSession, clinician_id, thread_id) -> AssistantThread:
    """Только свой тред: чужой/несуществующий неотличимы (404, R5 IDOR)."""
    thread = await db.get(AssistantThread, uuid.UUID(str(thread_id)))
    if thread is None or thread.clinician_id != uuid.UUID(str(clinician_id)):
        raise _not_found()
    return thread

async def _patient_ids(db: AsyncSession, thread_id) -> list[uuid.UUID]:
    return list(
        await db.scalars(
            select(AssistantThreadPatient.patient_id).where(
                AssistantThreadPatient.thread_id == thread_id
            )
        )
    )

async def _set_patients(db: AsyncSession, thread_id, patient_ids: list[uuid.UUID]) -> None:
    await db.execute(
        delete(AssistantThreadPatient).where(AssistantThreadPatient.thread_id == thread_id)
    )
    for pid in patient_ids:
        db.add(AssistantThreadPatient(thread_id=thread_id, patient_id=pid))
    # Сессия живёт с autoflush=False, а `_patient_ids` читает строки сразу после
    # этой записи в том же ходе: без явного flush новые пациенты треда не видны,
    # карты получаются пустыми и ассистент отвечает «данных о пациенте нет».
    await db.flush()

def _title(value: str | None) -> str:
    title = (value or "").strip()
    return title[:200] if title else "Новый диалог"

def _serialize(thread: AssistantThread, patient_ids, message_count: int = 0) -> dict:
    return {
        "id": str(thread.id),
        "title": thread.title,
        "patient_ids": [str(p) for p in patient_ids],
        "created_at": thread.created_at.isoformat() if thread.created_at else None,
        "updated_at": thread.updated_at.isoformat() if thread.updated_at else None,
        "message_count": message_count,
    }

def _serialize_message(m: AssistantMessage) -> dict:
    # answer_meta хранит тот же контракт, что ушёл клиенту в свежем ответе:
    # после перезагрузки треда UI не выдаёт детерминированные фрагменты за
    # ответ ИИ. У сообщений врача и у старых ответов meta пуста → None/[].
    meta = m.answer_meta or {}
    return {
        "id": str(m.id),
        "role": m.role,
        "content": m.content,
        "citations": list(m.citations or []),
        "topic": m.topic,
        "answer_kind": meta.get("answer_kind"),
        "llm_reason": meta.get("llm_reason"),
        "fragments": list(meta.get("fragments") or []),
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }

async def create_thread(db: AsyncSession, clinician_id, title: str | None, patient_ids) -> dict:
    ids = await normalize_patient_ids(db, clinician_id, patient_ids)
    thread = AssistantThread(clinician_id=uuid.UUID(str(clinician_id)), title=_title(title))
    db.add(thread)
    await db.flush()
    await _set_patients(db, thread.id, ids)
    await db.commit()
    await db.refresh(thread)
    return _serialize(thread, ids)

async def list_threads(db: AsyncSession, clinician_id) -> list[dict]:
    threads = (await db.scalars(
        select(AssistantThread)
        .where(AssistantThread.clinician_id == uuid.UUID(str(clinician_id)))
        .order_by(AssistantThread.updated_at.desc(), AssistantThread.id)
    )).all()
    if not threads:
        return []
    ids = [t.id for t in threads]
    counts = dict(
        (await db.execute(
            select(AssistantMessage.thread_id, func.count())
            .where(AssistantMessage.thread_id.in_(ids))
            .group_by(AssistantMessage.thread_id)
        )).all()
    )
    patients: dict[uuid.UUID, list[uuid.UUID]] = {i: [] for i in ids}
    for thread_id, patient_id in (await db.execute(
        select(AssistantThreadPatient.thread_id, AssistantThreadPatient.patient_id).where(
            AssistantThreadPatient.thread_id.in_(ids)
        )
    )).all():
        patients[thread_id].append(patient_id)
    return [_serialize(t, patients[t.id], int(counts.get(t.id, 0))) for t in threads]

async def get_thread_detail(db: AsyncSession, clinician_id, thread_id, ip: str | None = None) -> dict:
    thread = await get_thread(db, clinician_id, thread_id)
    ids = await _patient_ids(db, thread.id)
    messages = (await db.scalars(
        select(AssistantMessage)
        .where(AssistantMessage.thread_id == thread.id)
        .order_by(AssistantMessage.created_at, AssistantMessage.id)
    )).all()
    data = _serialize(thread, ids, len(messages))
    data["messages"] = [_serialize_message(m) for m in messages]
    if ids:
        # PHI-чтение: тред ссылается на пациентов → фиксируем доступ врача.
        await audit.record(
            db,
            uuid.UUID(str(clinician_id)),
            "READ_ASSISTANT_THREAD",
            "assistant_thread",
            str(thread.id),
            ip=ip,
        )
        await db.commit()
    return data

async def update_thread(
    db: AsyncSession,
    clinician_id,
    thread_id,
    *,
    title: str | None = None,
    patient_ids=None,
) -> dict:
    thread = await get_thread(db, clinician_id, thread_id)
    if title is not None:
        thread.title = _title(title)
    ids = await _patient_ids(db, thread.id)
    if patient_ids is not None:
        ids = await normalize_patient_ids(db, clinician_id, patient_ids)
        await _set_patients(db, thread.id, ids)
    thread.updated_at = func.now()
    await db.commit()
    await db.refresh(thread)
    count = await db.scalar(
        select(func.count())
        .select_from(AssistantMessage)
        .where(AssistantMessage.thread_id == thread.id)
    )
    return _serialize(thread, ids, int(count or 0))

async def delete_thread(db: AsyncSession, clinician_id, thread_id) -> None:
    thread = await get_thread(db, clinician_id, thread_id)
    await db.delete(thread)
    await db.commit()


# Разделитель реплик при батч-скрабе: пунктуация не даёт склеить соседние
# сущности, а после маскирования он же служит обратным разрезом.
HISTORY_SEP = "\n\n%%MSG%%\n\n"
# Потолок одного egress-текста: /v1/mask принимает ≤20k символов (ml/models.py),
# поэтому историю режем на пачки по размеру, а не всей портянкой.
HISTORY_SCRUB_CHARS = 6_000


def _chunk_texts(texts: list[str], limit: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for text in texts:
        extra = len(text) + (len(HISTORY_SEP) if current else 0)
        if current and size + extra > limit:
            chunks.append(current)
            current, size = [], 0
            extra = len(text)
        current.append(text)
        size += extra
    if current:
        chunks.append(current)
    return chunks


async def _history_for_llm(db: AsyncSession, thread_id, names: list[str]) -> list[dict]:
    """Последние реплики треда, деперсонализированные, oldest → newest.

    Для LLM-переформулировки уточняющего вопроса (FR-21): в промпт не должны
    попасть ФИО пациента/врача и прочие ПДн, поэтому реплики проходят
    scrub_for_llm ДО обрезки (обрезку по количеству/длине делает llm_router).
    Скрабим пачками: один и тот же deny-list/ML-детектор на пачку дешевле,
    чем запрос на каждую реплику.
    """
    rows = list((await db.scalars(
        select(AssistantMessage)
        .where(AssistantMessage.thread_id == thread_id)
        .order_by(AssistantMessage.created_at.desc(), AssistantMessage.id.desc())
        .limit(llm_router.REWRITE_HISTORY_MESSAGES)
    )).all())
    rows.reverse()
    if not rows:
        return []
    texts = [m.content or "" for m in rows]
    scrubbed: list[str] = []
    for chunk in _chunk_texts(texts, HISTORY_SCRUB_CHARS):
        masked = await anyio.to_thread.run_sync(
            scrub_for_llm, HISTORY_SEP.join(chunk), None, names
        )
        scrubbed.extend(masked[0].split(HISTORY_SEP))
    if len(scrubbed) != len(texts):
        # Разделитель не пережил маскирование — безопасный откат на поштучный
        # скраб: потерять реплику хуже, чем сделать лишний вызов.
        scrubbed = [
            (await anyio.to_thread.run_sync(scrub_for_llm, text, None, names))[0]
            for text in texts
        ]
    return [{"role": m.role, "content": text} for m, text in zip(rows, scrubbed)]


async def _scrub_patient_contexts(
    contexts: list[dict], names: list[str]
) -> tuple[list[dict], dict[str, int]]:
    """Маскирует свободный текст карт (ФИО, дневник) и сводит категории находок.

    Вопрос в контекст не входит, поэтому здесь передаётся пустой запрос: без
    этого тот же текст скрабился бы вторым и третьим вызовом (он уже прошёл
    scrub_for_llm как вопрос выше).
    """
    masked: list[dict] = []
    counts: dict[str, int] = {}
    for ctx in contexts:
        _, masked_ctx, report = await anyio.to_thread.run_sync(scrub_for_llm, "", ctx, names)
        masked.append(masked_ctx)
        for kind, number in report["masked"].items():
            counts[kind] = counts.get(kind, 0) + number
    return masked, counts


async def answer_query(
    db: AsyncSession,
    clinician_id,
    query: str,
    *,
    topic: str | None = None,
    limit: int = 5,
    patient_ids=None,
    ip: str | None = None,
) -> dict:
    """Одиночный запрос к ассистенту (POST /assistant/ask): карты пациентов +
    деперсонализация + RAG. Общий путь с тредом (FR-21), без истории."""
    # Ростер + ФИО самого врача: без этого его собственное ФИО в запросе
    # маскировалось только ML-детекцией, т.е. зависело от backend'а.
    names = await deny_names_for_llm(db, clinician_id, clinician_id)
    patient_contexts = [
        await clinician.patient_context(db, clinician_id, pid, ip=ip) for pid in patient_ids or []
    ]
    exclude_ids = await kb_review.excluded_ids(db, clinician_id)
    # FR-36: статьи видит только тот врач, кто одобрил их лично (пустое
    # множество — ни одной статьи; гайдлайны ВОЗ/seed идут мимо фильтра).
    allowed_work_ids = await kb_review.approved_work_ids(db, clinician_id)
    corpus_drugs = await load_drug_docs(db)
    await db.commit()  # PDn-access audit персистируется до вызова агента (152-ФЗ)

    scrubbed_query, _, report = await anyio.to_thread.run_sync(
        scrub_for_llm, query, None, names
    )
    scrubbed_contexts, context_findings = await _scrub_patient_contexts(patient_contexts, names)
    if context_findings:
        report["applied"] = True
        for kind, number in context_findings.items():
            report["masked"][kind] = report["masked"].get(kind, 0) + number
    result = await _invoke_agent(
        scrubbed_query,
        topic=topic,
        limit=limit,
        patient_contexts=scrubbed_contexts or None,
        exclude_ids=exclude_ids,
        allowed_work_ids=allowed_work_ids,
        corpus_drugs=corpus_drugs,
    )
    result["depersonalization"] = report
    return result

async def _prepare_turn(
    db: AsyncSession,
    clinician_id,
    thread_id,
    content: str,
    *,
    patient_ids=None,
    ip: str | None = None,
) -> _Turn:
    """Тред, история, вопрос и очищенный поисковый запрос до вызова агента."""
    thread = await get_thread(db, clinician_id, thread_id)
    if patient_ids is not None:
        await _set_patients(db, thread.id, await normalize_patient_ids(db, clinician_id, patient_ids))
        thread.updated_at = func.now()
    ids = await _patient_ids(db, thread.id)

    # Уточняющий вопрос («а в каких дозах?») сам по себе не ищется: retrieval и
    # роутинг идут по LLM-переформулировке последних реплик треда. История в
    # embedding НЕ конкатенируется (ФИО/redline-реплики сдвигали поиск), а в LLM
    # уходит только после scrub_for_llm. Читаем её до вставки текущего вопроса —
    # это прошлые реплики, не он сам.
    names = await deny_names_for_llm(db, clinician_id, clinician_id)
    history = await _history_for_llm(db, thread.id, names)

    question = AssistantMessage(
        thread_id=thread.id, role="CLINICIAN", content=content, citations=[]
    )
    db.add(question)
    await db.flush()
    contexts: list[dict] = []
    skipped: list[str] = []
    for pid in ids:
        try:
            # roster-gate + согласие + audit ASK_ASSISTANT_WITH_PATIENT
            contexts.append(await clinician.patient_context(db, clinician_id, pid, ip=ip))
        except Problem:
            # Врач потерял доступ (roster/согласие) — тред не ломаем, сообщаем.
            skipped.append(str(pid))
    exclude_ids = await kb_review.excluded_ids(db, clinician_id)
    # FR-36: персональная видимость статей — тот же allow-list, что в /ask.
    allowed_work_ids = await kb_review.approved_work_ids(db, clinician_id)
    corpus_drugs = await load_drug_docs(db)
    await db.commit()  # PDn-access audit + вопрос персистируются до вызова агента

    scrubbed_query, _, _ = await anyio.to_thread.run_sync(
        scrub_for_llm, content, None, names
    )
    scrubbed_contexts, _ = await _scrub_patient_contexts(contexts, names)

    # Одиночный вопрос (истории нет) LLM не вызывает: переформулировать нечего.
    search_query = scrubbed_query
    rewritten = False
    # Redline-вопрос не ищется вовсе (детерминированный отказ) — не тратим LLM
    # и не отправляем такой текст в модель; red line считается по оригиналу.
    if history and detect_redline(content) is None:
        candidate = await anyio.to_thread.run_sync(
            llm_router.rewrite_followup, history, scrubbed_query
        )
        if candidate:
            search_query, rewritten = candidate, True

    return _Turn(
        thread=thread,
        ids=ids,
        history=history,
        question=question,
        skipped=skipped,
        exclude_ids=exclude_ids,
        allowed_work_ids=allowed_work_ids,
        corpus_drugs=corpus_drugs,
        scrubbed_query=scrubbed_query,
        scrubbed_contexts=scrubbed_contexts,
        search_query=search_query,
        rewritten=rewritten,
    )


async def _store_answer(
    db: AsyncSession, clinician_id, turn: _Turn, result: dict, *, ip: str | None
) -> dict:
    """Ответ агента → AssistantMessage + аудит; возвращает контракт ручки."""
    answer_meta = {
        "answer_kind": result.get("answer_kind"),
        "llm_reason": result.get("llm_reason"),
        "fragments": result.get("fragments") or [],
    }
    answer = AssistantMessage(
        thread_id=turn.thread.id,
        role="ASSISTANT",
        content=result["answer"],
        citations=result.get("citations") or [],
        topic=result.get("topic"),
        answer_meta=answer_meta,
    )
    db.add(answer)
    if not turn.ids:
        await audit.record(
            db, uuid.UUID(str(clinician_id)), "ASK_ASSISTANT", "kb", None, ip=ip
        )
    elif turn.skipped:
        # Часть карт недоступна (roster/согласие), но расшифрованная история
        # треда всё равно читается → фиксируем PHI-доступ.
        await audit.record(
            db,
            uuid.UUID(str(clinician_id)),
            "READ_ASSISTANT_THREAD",
            "assistant_thread",
            str(turn.thread.id),
            ip=ip,
        )
    turn.thread.updated_at = func.now()
    await db.commit()
    await db.refresh(turn.question)
    await db.refresh(answer)
    return {
        "answer": answer.content,
        "citations": list(answer.citations or []),
        "topic": answer.topic,
        # Признак «ответ ИИ / детерминированные фрагменты»: UI не выдаёт
        # сырую выдачу сниппетов за ответ модели (docs/03 §12).
        "answer_kind": answer_meta["answer_kind"],
        "llm_reason": answer_meta["llm_reason"],
        "fragments": answer_meta["fragments"],
        "grounded": result.get("grounded", False),
        "refused": result.get("refused", False),
        "refusal_code": result.get("refusal_code"),
        "skipped_patient_ids": turn.skipped,
        "message_ids": [str(turn.question.id), str(answer.id)],
        # Основание поиска для врача: что реально искали и была ли LLM-переформулировка.
        "retrieval_query": turn.search_query,
        "rewritten": turn.rewritten,
    }


async def post_message(
    db: AsyncSession,
    clinician_id,
    thread_id,
    content: str,
    *,
    patient_ids=None,
    ip: str | None = None,
) -> dict:
    """Вопрос врача → ответ RAG с учётом истории треда и карт пациентов."""
    turn = await _prepare_turn(
        db, clinician_id, thread_id, content, patient_ids=patient_ids, ip=ip
    )
    result = await _invoke_agent(
        turn.scrubbed_query,
        retrieval_query=turn.search_query if turn.rewritten else None,
        limit=5,
        patient_contexts=turn.scrubbed_contexts or None,
        exclude_ids=turn.exclude_ids,
        allowed_work_ids=turn.allowed_work_ids,
        corpus_drugs=turn.corpus_drugs,
        # История треда — контекст для LLM-ответа (FR-17); она уже прошла
        # scrub_for_llm в _history_for_llm, карты пациентов сюда не входят.
        history=turn.history,
    )
    return await _store_answer(db, clinician_id, turn, result, ip=ip)


async def post_message_stream(
    db: AsyncSession,
    clinician_id,
    thread_id,
    content: str,
    *,
    patient_ids=None,
    ip: str | None = None,
) -> AsyncIterator[dict]:
    """То же, что post_message, но текст ответа идёт событиями SSE.

    События: meta (что искали), token ({"t": дельта}), done (полный
    AssistantThreadAnswer). Ошибка до/во время потока — событие error в ручке.
    """
    turn = await _prepare_turn(
        db, clinician_id, thread_id, content, patient_ids=patient_ids, ip=ip
    )
    yield {
        "event": "meta",
        "data": {
            "skipped_patient_ids": turn.skipped,
            "retrieval_query": turn.search_query,
            "rewritten": turn.rewritten,
        },
    }
    result: dict | None = None
    async for item in _stream_agent(
        turn.scrubbed_query,
        retrieval_query=turn.search_query if turn.rewritten else None,
        limit=5,
        patient_contexts=turn.scrubbed_contexts or None,
        exclude_ids=turn.exclude_ids,
        allowed_work_ids=turn.allowed_work_ids,
        corpus_drugs=turn.corpus_drugs,
        history=turn.history,
    ):
        if "delta" in item:
            yield {"event": "token", "data": {"t": item["delta"]}}
        else:
            result = item["answer"]
    if result is None:
        raise Problem(500, "ASSISTANT_NO_ANSWER", "Ассистент не вернул ответ")
    payload = await _store_answer(db, clinician_id, turn, result, ip=ip)
    yield {"event": "done", "data": payload}
