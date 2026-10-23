"""Сессии клинического ассистента врача (FR-21).

Тред хранит контекст диалога между запросами и область пациентов (мультипациент).
Ответ по-прежнему собирается детерминированным RAG-путём (services.agent), но
уточняющий вопрос («а в каких дозах?») ищется по LLM-переформулировке последних
реплик треда. История в embedding НЕ конкатенируется (ФИО/redline-реплики из
истории сдвигали поиск): переформулировка используется только для retrieval и
выбора раздела, red line, ответ и цитаты считаются по исходному вопросу (FR-21).

ПДн: текст сообщений — спецкатегория (EncryptedText), карты пациентов попадают
в контекст только через clinician.patient_context (roster-gate + согласие).
Обращение к треду с пациентами аудируется READ_ASSISTANT_THREAD.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from compass.agent import detect_redline

from app.enrichment import review as kb_review
from app.errors import Problem
from app.models import (
    AssistantMessage,
    AssistantThread,
    AssistantThreadPatient,
    ClinicianRoster,
)
from app.services import audit, clinician, llm_router
from app.services.agent import scrub_for_llm, shared_factory

MAX_THREAD_PATIENTS = 10

def _not_found() -> Problem:
    return Problem(404, "THREAD_NOT_FOUND", "Диалог не найден")

def normalize_patient_ids(db: Session, clinician_id, patient_ids) -> list[uuid.UUID]:
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
            db.scalars(
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

def get_thread(db: Session, clinician_id, thread_id) -> AssistantThread:
    """Только свой тред: чужой/несуществующий неотличимы (404, R5 IDOR)."""
    thread = db.get(AssistantThread, uuid.UUID(str(thread_id)))
    if thread is None or thread.clinician_id != uuid.UUID(str(clinician_id)):
        raise _not_found()
    return thread

def _patient_ids(db: Session, thread_id) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(AssistantThreadPatient.patient_id).where(
                AssistantThreadPatient.thread_id == thread_id
            )
        )
    )

def _set_patients(db: Session, thread_id, patient_ids: list[uuid.UUID]) -> None:
    db.execute(
        delete(AssistantThreadPatient).where(AssistantThreadPatient.thread_id == thread_id)
    )
    for pid in patient_ids:
        db.add(AssistantThreadPatient(thread_id=thread_id, patient_id=pid))

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
    return {
        "id": str(m.id),
        "role": m.role,
        "content": m.content,
        "citations": list(m.citations or []),
        "topic": m.topic,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }

def create_thread(db: Session, clinician_id, title: str | None, patient_ids) -> dict:
    ids = normalize_patient_ids(db, clinician_id, patient_ids)
    thread = AssistantThread(clinician_id=uuid.UUID(str(clinician_id)), title=_title(title))
    db.add(thread)
    db.flush()
    _set_patients(db, thread.id, ids)
    db.commit()
    db.refresh(thread)
    return _serialize(thread, ids)

def list_threads(db: Session, clinician_id) -> list[dict]:
    threads = db.scalars(
        select(AssistantThread)
        .where(AssistantThread.clinician_id == uuid.UUID(str(clinician_id)))
        .order_by(AssistantThread.updated_at.desc(), AssistantThread.id)
    ).all()
    if not threads:
        return []
    ids = [t.id for t in threads]
    counts = dict(
        db.execute(
            select(AssistantMessage.thread_id, func.count())
            .where(AssistantMessage.thread_id.in_(ids))
            .group_by(AssistantMessage.thread_id)
        ).all()
    )
    patients: dict[uuid.UUID, list[uuid.UUID]] = {i: [] for i in ids}
    for thread_id, patient_id in db.execute(
        select(AssistantThreadPatient.thread_id, AssistantThreadPatient.patient_id).where(
            AssistantThreadPatient.thread_id.in_(ids)
        )
    ).all():
        patients[thread_id].append(patient_id)
    return [_serialize(t, patients[t.id], int(counts.get(t.id, 0))) for t in threads]

def get_thread_detail(db: Session, clinician_id, thread_id, ip: str | None = None) -> dict:
    thread = get_thread(db, clinician_id, thread_id)
    ids = _patient_ids(db, thread.id)
    messages = db.scalars(
        select(AssistantMessage)
        .where(AssistantMessage.thread_id == thread.id)
        .order_by(AssistantMessage.created_at, AssistantMessage.id)
    ).all()
    data = _serialize(thread, ids, len(messages))
    data["messages"] = [_serialize_message(m) for m in messages]
    if ids:
        # PHI-чтение: тред ссылается на пациентов → фиксируем доступ врача.
        audit.record(
            db,
            uuid.UUID(str(clinician_id)),
            "READ_ASSISTANT_THREAD",
            "assistant_thread",
            str(thread.id),
            ip=ip,
        )
        db.commit()
    return data

def update_thread(
    db: Session,
    clinician_id,
    thread_id,
    *,
    title: str | None = None,
    patient_ids=None,
) -> dict:
    thread = get_thread(db, clinician_id, thread_id)
    if title is not None:
        thread.title = _title(title)
    ids = _patient_ids(db, thread.id)
    if patient_ids is not None:
        ids = normalize_patient_ids(db, clinician_id, patient_ids)
        _set_patients(db, thread.id, ids)
    thread.updated_at = func.now()
    db.commit()
    db.refresh(thread)
    count = db.scalar(
        select(func.count())
        .select_from(AssistantMessage)
        .where(AssistantMessage.thread_id == thread.id)
    )
    return _serialize(thread, ids, int(count or 0))

def delete_thread(db: Session, clinician_id, thread_id) -> None:
    thread = get_thread(db, clinician_id, thread_id)
    db.delete(thread)
    db.commit()


def _history_for_llm(db: Session, thread_id, names: list[str]) -> list[dict]:
    """Последние реплики треда, деперсонализированные, oldest → newest.

    Для LLM-переформулировки уточняющего вопроса (FR-21): в промпт не должны
    попасть ФИО пациента/врача и прочие ПДн, поэтому каждая реплика проходит
    scrub_for_llm. Обрезку по количеству/длине делает llm_router.
    """
    rows = db.scalars(
        select(AssistantMessage)
        .where(AssistantMessage.thread_id == thread_id)
        .order_by(AssistantMessage.created_at.desc(), AssistantMessage.id.desc())
        .limit(llm_router.REWRITE_HISTORY_MESSAGES)
    ).all()
    return [
        {"role": m.role, "content": scrub_for_llm(m.content, None, names)[0]}
        for m in reversed(rows)
    ]

def answer_query(
    db: Session,
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
    names = clinician.roster_names(db, clinician_id)
    patient_contexts = [
        clinician.patient_context(db, clinician_id, pid, ip=ip) for pid in patient_ids or []
    ]
    db.commit()  # PDn-access audit персистируется до вызова агента (152-ФЗ)

    scrubbed_query, _, report = scrub_for_llm(
        query, patient_contexts[0] if patient_contexts else None, names
    )
    scrubbed_contexts = [scrub_for_llm(query, ctx, names)[1] for ctx in patient_contexts]
    result = shared_factory().invoke(
        scrubbed_query,
        topic=topic,
        limit=limit,
        patient_contexts=scrubbed_contexts or None,
        exclude_ids=kb_review.excluded_ids(db, clinician_id),
    )
    result["depersonalization"] = report
    return result

def post_message(
    db: Session,
    clinician_id,
    thread_id,
    content: str,
    *,
    patient_ids=None,
    ip: str | None = None,
) -> dict:
    """Вопрос врача → ответ RAG с учётом истории треда и карт пациентов."""
    thread = get_thread(db, clinician_id, thread_id)
    if patient_ids is not None:
        _set_patients(db, thread.id, normalize_patient_ids(db, clinician_id, patient_ids))
        thread.updated_at = func.now()
    ids = _patient_ids(db, thread.id)

    # Уточняющий вопрос («а в каких дозах?») сам по себе не ищется: retrieval и
    # роутинг идут по LLM-переформулировке последних реплик треда. История в
    # embedding НЕ конкатенируется (ФИО/redline-реплики сдвигали поиск), а в LLM
    # уходит только после scrub_for_llm. Читаем её до вставки текущего вопроса —
    # это прошлые реплики, не он сам.
    names = clinician.roster_names(db, clinician_id)
    history = _history_for_llm(db, thread.id, names)

    question = AssistantMessage(
        thread_id=thread.id, role="CLINICIAN", content=content, citations=[]
    )
    db.add(question)
    db.flush()
    contexts: list[dict] = []
    skipped: list[str] = []
    for pid in ids:
        try:
            # roster-gate + согласие + audit ASK_ASSISTANT_WITH_PATIENT
            contexts.append(clinician.patient_context(db, clinician_id, pid, ip=ip))
        except Problem:
            # Врач потерял доступ (roster/согласие) — тред не ломаем, сообщаем.
            skipped.append(str(pid))
    db.commit()  # PDn-access audit + вопрос персистируются до вызова агента

    scrubbed_query, _, _ = scrub_for_llm(content, None, names)
    scrubbed_contexts = [scrub_for_llm(content, ctx, names)[1] for ctx in contexts]

    # Одиночный вопрос (истории нет) LLM не вызывает: переформулировать нечего.
    search_query = scrubbed_query
    rewritten = False
    # Redline-вопрос не ищется вовсе (детерминированный отказ) — не тратим LLM
    # и не отправляем такой текст в модель; red line считается по оригиналу.
    if history and detect_redline(content) is None:
        candidate = llm_router.rewrite_followup(history, scrubbed_query)
        if candidate:
            search_query, rewritten = candidate, True

    result = shared_factory().invoke(
        scrubbed_query,
        retrieval_query=search_query if rewritten else None,
        limit=5,
        patient_contexts=scrubbed_contexts or None,
        exclude_ids=kb_review.excluded_ids(db, clinician_id),
    )

    answer = AssistantMessage(
        thread_id=thread.id,
        role="ASSISTANT",
        content=result["answer"],
        citations=result.get("citations") or [],
        topic=result.get("topic"),
    )
    db.add(answer)
    if not ids:
        audit.record(
            db, uuid.UUID(str(clinician_id)), "ASK_ASSISTANT", "kb", None, ip=ip
        )
    elif skipped:
        # Часть карт недоступна (roster/согласие), но расшифрованная история
        # треда всё равно читается → фиксируем PHI-доступ.
        audit.record(
            db,
            uuid.UUID(str(clinician_id)),
            "READ_ASSISTANT_THREAD",
            "assistant_thread",
            str(thread.id),
            ip=ip,
        )
    thread.updated_at = func.now()
    db.commit()
    db.refresh(question)
    db.refresh(answer)
    return {
        "answer": answer.content,
        "citations": list(answer.citations or []),
        "topic": answer.topic,
        "grounded": result.get("grounded", False),
        "refused": result.get("refused", False),
        "refusal_code": result.get("refusal_code"),
        "skipped_patient_ids": skipped,
        "message_ids": [str(question.id), str(answer.id)],
        # Основание поиска для врача: что реально искали и была ли LLM-переформулировка.
        "retrieval_query": search_query,
        "rewritten": rewritten,
    }
