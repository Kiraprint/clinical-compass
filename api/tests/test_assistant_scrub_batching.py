"""Деперсонализация в треде: вопрос и история — без лишних вызовов ML.

Контракт docs/06 §3, docs/12. На родительском коде история скрабилась
по сообщению (до 8 запросов в /v1/mask), а вопрос — дважды.
"""

from __future__ import annotations

import uuid

from app import db
from app.models import AssistantMessage, AssistantThread, User
from app.services import assistant_chat

from .conftest import run_async


def _thread_with_messages(n: int) -> uuid.UUID:
    with db.SyncSessionLocal() as s:
        user = User(
            email=f"scrub-{uuid.uuid4().hex[:8]}@test.ru", password_hash="x",
            role="CLINICIAN", totp_secret="SECRET",
        )
        s.add(user)
        s.flush()
        thread = AssistantThread(clinician_id=user.id, title="Тред")
        s.add(thread)
        s.flush()
        for i in range(n):
            s.add(AssistantMessage(
                thread_id=thread.id, role="CLINICIAN" if i % 2 == 0 else "ASSISTANT",
                content=f"Сообщение {i}: жалобы у Иванова Ивана",
            ))
        s.commit()
        return thread.id


def test_history_is_scrubbed_in_one_batch(engine, monkeypatch):
    """5 реплик → 1 вызов скраба (было 5), порядок и число реплик сохранены."""
    thread_id = _thread_with_messages(5)
    calls: list[str] = []
    real = assistant_chat.scrub_for_llm

    def _counting(query, patient_context, names=None):
        calls.append(query)
        return real(query, patient_context, names)

    monkeypatch.setattr(assistant_chat, "scrub_for_llm", _counting)
    history = run_async(assistant_chat._history_for_llm, thread_id, ["Иванов Иван"])

    assert len(calls) == 1, calls
    assert len(history) == 5
    assert [m["role"] for m in history].count("ASSISTANT") == 2
    # Ни одна реплика не потеряна и не продублирована (created_at у реплик
    # одинаковый, поэтому порядок внутри пачки не фиксирован), ПДн скрыты.
    seen = sorted(
        int(message["content"].split("Сообщение ")[1].split(":")[0]) for message in history
    )
    assert seen == [0, 1, 2, 3, 4], history
    assert all("Иванов" not in message["content"] for message in history), history


def test_chunks_keep_every_message(engine):
    """Разбиение по размеру не теряет реплики: склейка/разрез — обратимые."""
    texts = [f"текст {i}" for i in range(7)]
    chunks = assistant_chat._chunk_texts(texts, 60)
    assert sum(len(c) for c in chunks) == len(texts)
    assert [t for c in chunks for t in c] == texts
