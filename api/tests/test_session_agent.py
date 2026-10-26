"""FR-44 (docs/18 §10): протокол как тул агента, автодрафт и диалог по сессии.

Проверяется: тул черновика возвращает только сводку (без текста разделов) и не
даёт доступа к чужой сессии; 409 PROTOCOL_LOCKED на утверждённом протоколе;
автодрафт после расшифровки (включён / выключен / сбой модели); чат — ответ с
цитатами, `proposal` без мутации протокола, гейты 403/404/503, потолок длины
сообщения, шифрование реплик в БД и маскирование ПДн до LLM.

Сеть не нужна: ML-сайдкар и LLM подменяются на уровне сервисов.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from compass.agent import RAGChunk

from app import db as app_db
from app.config import settings
from app.errors import Problem
from app.services import llm_router, session_agent, session_ai

from .conftest import run_async
from .test_session_ai import (  # noqa: F401 — общие фикстуры/хелперы модуля FR-39
    AUDIO,
    CLINICIAN_FIO,
    PATIENT_FIO,
    PATIENT_PHONE,
    _ctx,
    _detail,
    _fake_ml,
    _fio,
    _session,
    _transcribed,
    _upload,
    _wait_ai_status,
)

BASE = "/api/v1/clinician"

DRAFT_JSON = json.dumps(
    {
        "complaints": "тревога, нарушение сна",
        "anamnesis_morbi": "впервые отметил после инфекции",
        "mental_status": {
            "appearance_behavior": "опрятен",
            "consciousness_orientation": "ориентирован",
            "perception": "без обманов",
            "thinking_speech": "темп обычный",
            "emotions_volition": "тревожен",
            "criticism": "частичная",
            "suicidal_risk": "отрицает",
        },
        "somatic_status": "без особенностей",
        "recommendations": "КПТ, контроль через 2 недели",
    },
    ensure_ascii=False,
)

FULL = {
    "passport": {
        "visit_at": "2026-10-26T10:00:00+03:00",
        "channel": "in_person",
        "legal_basis": "ИДС № 12 от 2026-05-04",
    },
    "complaints": "тревога, бессонница",
    "anamnesis_morbi": "впервые в 2024",
    "mental_status": {
        "appearance_behavior": "опрятен",
        "consciousness_orientation": "ориентирован",
        "perception": "без обманов",
        "thinking_speech": "темп обычный",
        "emotions_volition": "тревожен",
        "criticism": "частичная",
        "suicidal_risk": "отрицает",
    },
    "somatic_status": "без особенностей",
    "diagnosis": {"syndrome": "тревожный", "icd10": "F41.1", "rationale": "по МКБ-10"},
    "treatment": [
        {"drug": "сертралин", "dose": "50 мг", "schedule": "утром", "duration": "6 мес"}
    ],
    "recommendations": "КПТ, контроль через 2 недели",
}

CHUNK = RAGChunk(
    text="При генерализованном тревожном расстройстве сертралин 50 мг утром.",
    source_title="Рекомендации по ГТР",
    guideline_id="gtr-2024",
    topic="ЛЕКАРСТВА",
    score=0.91,
    chunk_id="chunk-1",
)


class _FakeKb:
    def __init__(self, chunks: list[RAGChunk] | None = None) -> None:
        self.chunks = list(chunks or [])
        self.calls: list[dict] = []

    def search_kb(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return list(self.chunks)


def _kb(monkeypatch, chunks: list[RAGChunk] | None = None) -> _FakeKb:
    """Одна и та же подмена поиска для обоих модулей (импорт по имени)."""
    fake = _FakeKb(chunks)
    monkeypatch.setattr(session_ai, "shared_factory", lambda: fake)
    monkeypatch.setattr(session_agent, "shared_factory", lambda: fake)
    return fake


def _draft_llm(monkeypatch, content: str = DRAFT_JSON) -> dict:
    """LLM черновика: `chat_completion` (session_ai._ask)."""
    captured: dict = {"calls": 0}

    def fake(messages, *, temperature=0.0, timeout=None):
        captured["calls"] += 1
        captured["messages"] = messages
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    monkeypatch.setattr(llm_router, "chat_completion", fake)
    return captured


def _chat_llm(monkeypatch, *, content: str, tool_calls: list[dict] | None = None,
              aggregate: list[dict] | None = None) -> dict:
    """tool-loop: первый вызов с тулами — вызовы тулз, дальше — JSON-ответ."""
    state: dict = {"rounds": 0, "payloads": aggregate if aggregate is not None else []}

    def fake(messages, tools=None, *, temperature=0.0, timeout=None):
        state["rounds"] += 1
        state["payloads"].append(
            {"messages": json.loads(json.dumps(messages, ensure_ascii=False)),
             "tools": [t["function"]["name"] for t in (tools or [])]}
        )
        if tools and tool_calls and state["rounds"] == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": tool_calls,
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    monkeypatch.setattr(llm_router, "chat_with_tools", fake)
    return state


def _compose(monkeypatch, answer_text: str = "Сертралин 50 мг утром [1].") -> dict:
    state: dict = {"calls": 0}

    def fake(history, query, chunks):
        state["calls"] += 1
        state["history"] = history
        state["query"] = query
        return answer_text

    monkeypatch.setattr(llm_router, "compose_answer", fake)
    return state


def _put_protocol(client, ctx, session_id: str) -> dict:
    resp = client.put(
        f"{BASE}/sessions/{session_id}/protocol",
        json={"sections": FULL, "diagnosis_code": "F41.1"},
        headers=ctx["doctor"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    """ИИ включён, автодрафт выключен: каждый тест включает его сам."""
    monkeypatch.setattr(settings, "session_ai_enabled", True)
    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    monkeypatch.setattr(settings, "prod_mode", False)
    monkeypatch.setattr(settings, "depersonalize_ner", "off")
    monkeypatch.setattr(settings, "session_audio_max_mb", 200)
    monkeypatch.setattr(settings, "session_ai_autodraft", False)
    monkeypatch.setattr(settings, "session_chat_message_chars", 2000)
    monkeypatch.setattr(settings, "session_chat_max_turns", 100)


# --- 1. тул черновика --------------------------------------------------------


def test_tool_creates_draft_and_returns_compact_summary(client, monkeypatch, db_session):
    _kb(monkeypatch)
    _draft_llm(monkeypatch)
    ctx = _ctx(client, "tool")
    session, _ = _transcribed(client, ctx, monkeypatch)

    result = run_async(
        session_agent.draft_session_protocol,
        uuid.UUID(ctx["clinician_id"]),
        session["id"],
        bound_session_id=session["id"],
    )
    # Контракт тула: только сводка, текста протокола в контексте модели нет.
    assert set(result) == {
        "protocol_id",
        "version",
        "filled_sections",
        "needs_review_count",
        "sources_count",
    }
    assert result["version"] == 1
    assert "complaints" in result["filled_sections"]
    assert "тревога" not in json.dumps(result, ensure_ascii=False)
    assert result["sources_count"] == 0

    db_session.expire_all()
    row = db_session.execute(
        text("select source, status from session_protocols where session_id = :i"),
        {"i": session["id"]},
    ).one()
    assert (row.source, row.status) == ("llm", "draft")
    # Аудит: код отличает вызов тулом от кнопки и от автодрафта — только ids.
    action = db_session.execute(
        text("select action, object_type, object_id from audit_log "
             "where action = 'SESSION_PROTOCOL_CHAT_DRAFT' order by id desc")
    ).first()
    assert action is not None
    assert action.object_type == "session_protocol"
    assert action.object_id.startswith(session["id"])


def test_tool_foreign_session_id_is_not_a_door(client, monkeypatch):
    _kb(monkeypatch)
    _draft_llm(monkeypatch)
    ctx = _ctx(client, "toolforeign")
    mine, _ = _transcribed(client, ctx, monkeypatch)
    other, _ = _transcribed(client, ctx, monkeypatch)

    with pytest.raises(Problem) as caught:
        run_async(
            session_agent.draft_session_protocol,
            uuid.UUID(ctx["clinician_id"]),
            other["id"],
            bound_session_id=mine["id"],
        )
    assert caught.value.status == 404
    assert caught.value.code == "SESSION_NOT_FOUND"

    with pytest.raises(Problem):
        run_async(
            session_agent.draft_session_protocol,
            uuid.UUID(ctx["clinician_id"]),
            "не-uuid",
            bound_session_id=mine["id"],
        )


def test_tool_refuses_locked_protocol(client, monkeypatch, db_session):
    _kb(monkeypatch)
    _draft_llm(monkeypatch)
    ctx = _ctx(client, "toollock")
    session, _ = _transcribed(client, ctx, monkeypatch)
    _put_protocol(client, ctx, session["id"])
    approved = client.post(
        f"{BASE}/sessions/{session['id']}/protocol/approve", headers=ctx["doctor"]
    )
    assert approved.status_code == 200, approved.text

    with pytest.raises(Problem) as caught:
        run_async(
            session_agent.draft_session_protocol,
            uuid.UUID(ctx["clinician_id"]),
            session["id"],
            bound_session_id=session["id"],
        )
    assert caught.value.status == 409
    assert caught.value.code == "PROTOCOL_LOCKED"


# --- 2. автодрафт ------------------------------------------------------------


def test_autodraft_runs_after_transcription(client, monkeypatch, db_session):
    monkeypatch.setattr(settings, "session_ai_autodraft", True)
    _kb(monkeypatch)
    _draft_llm(monkeypatch)
    ctx = _ctx(client, "auto")
    session, _ = _transcribed(client, ctx, monkeypatch)

    deadline = time.time() + 15
    version = None
    while time.time() < deadline:
        db_session.expire_all()
        version = db_session.execute(
            text("select version from session_protocols where session_id = :i"),
            {"i": session["id"]},
        ).scalar()
        if version:
            break
        time.sleep(0.02)
    assert version == 1
    action = db_session.execute(
        text("select action from audit_log where action like 'SESSION_PROTOCOL_%DRAFT%' "
             "order by id desc")
    ).first()
    assert action.action == "SESSION_PROTOCOL_AUTODRAFT"
    detail = _detail(client, ctx["doctor"], session["id"])
    assert detail["ai_status"] == "transcribed"
    assert detail["ai_error"] is None


def test_autodraft_disabled_leaves_no_draft(client, monkeypatch, db_session):
    _kb(monkeypatch)
    _draft_llm(monkeypatch)
    ctx = _ctx(client, "autoff")
    session, _ = _transcribed(client, ctx, monkeypatch)
    time.sleep(0.2)
    db_session.expire_all()
    count = db_session.execute(
        text("select count(*) from session_protocols where session_id = :i"),
        {"i": session["id"]},
    ).scalar()
    assert count == 0


def test_autodraft_failure_keeps_transcript(client, monkeypatch, db_session):
    monkeypatch.setattr(settings, "session_ai_autodraft", True)
    _kb(monkeypatch)

    def broken(messages, *, temperature=0.0, timeout=None):
        # Сбой модели: невалидный JSON вместо разделов протокола.
        return {"choices": [{"message": {"role": "assistant", "content": "не json"}}]}

    monkeypatch.setattr(llm_router, "chat_completion", broken)
    ctx = _ctx(client, "autofail")
    session, _ = _transcribed(client, ctx, monkeypatch)

    deadline = time.time() + 15
    error = None
    while time.time() < deadline:
        db_session.expire_all()
        error = db_session.execute(
            text("select ai_error from clinical_sessions where id = :i"),
            {"i": session["id"]},
        ).scalar()
        if error:
            break
        time.sleep(0.02)
    assert error == "DRAFT_FAILED"
    detail = _detail(client, ctx["doctor"], session["id"])
    # Расшифровка состоялась: сбой автодрафта не переводит сессию в failed.
    assert detail["ai_status"] == "transcribed"
    transcript = client.get(f"{BASE}/sessions/{session['id']}/transcript",
                            headers=ctx["doctor"])
    assert transcript.status_code == 200


# --- 3. чат по сессии --------------------------------------------------------


def _chat(client, ctx, session_id, message, *, headers=None):
    return client.post(
        f"{BASE}/sessions/{session_id}/chat",
        json={"message": message},
        headers=headers or ctx["doctor"],
    )


def test_chat_answer_grounded_with_citations(client, monkeypatch, db_session):
    _kb(monkeypatch, [CHUNK])
    _chat_llm(monkeypatch, content=json.dumps({"answer": "см. сниппет", "proposal": None}))
    _compose(monkeypatch)
    ctx = _ctx(client, "chat")
    _fio(db_session, ctx)
    session = _session(client, ctx)

    resp = _chat(client, ctx, session["id"], "что сказано про сертралин и тревогу?")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "assistant"
    assert body["citations"] and body["citations"][0]["guideline_id"] == "gtr-2024"
    assert "[1]" in body["content"]
    assert body["proposal"] is None

    hist = client.get(f"{BASE}/sessions/{session['id']}/chat", headers=ctx["doctor"])
    assert hist.status_code == 200, hist.text
    items = hist.json()["items"]
    assert [m["role"] for m in items] == ["clinician", "assistant"]
    assert items[0]["content"].startswith("что сказано про сертралин")

    db_session.expire_all()
    actions = [
        row.action
        for row in db_session.execute(
            text("select action from audit_log where object_id = :i"), {"i": session["id"]}
        )
    ]
    assert "SESSION_CHAT_ASK" in actions


def test_chat_proposal_does_not_mutate_protocol(client, monkeypatch):
    _kb(monkeypatch, [CHUNK])
    _chat_llm(
        monkeypatch,
        content=json.dumps(
            {
                "answer": "предлагаю уточнить контроль",
                "proposal": {
                    "section": "recommendations",
                    "after": "КПТ, контроль через 4 недели",
                    "reason": "в источнике указан месячный контроль",
                    "sources": [1],
                },
            },
            ensure_ascii=False,
        ),
    )
    _compose(monkeypatch)
    ctx = _ctx(client, "chatprop")
    session = _session(client, ctx)
    before = _put_protocol(client, ctx, session["id"])

    resp = _chat(client, ctx, session["id"], "поправь периодичность контроля")
    assert resp.status_code == 200, resp.text
    proposal = resp.json()["proposal"]
    assert proposal["section"] == "recommendations"
    assert proposal["before"] == FULL["recommendations"]
    assert proposal["after"] == "КПТ, контроль через 4 недели"
    assert proposal["sources"][0]["source_title"] == CHUNK.source_title

    after = client.get(f"{BASE}/sessions/{session['id']}/protocol", headers=ctx["doctor"]).json()
    assert after["sections"]["recommendations"] == FULL["recommendations"]
    assert after["version"] == before["version"]
    assert after["updated_at"] == before["updated_at"]


def test_chat_rejects_unsupported_proposal_section(client, monkeypatch):
    _kb(monkeypatch, [CHUNK])
    _chat_llm(
        monkeypatch,
        content=json.dumps(
            {
                "answer": "…",
                "proposal": {
                    "section": "treatment",
                    "after": "сертралин 100 мг",
                    "reason": "x",
                    "sources": [1],
                },
            }
        ),
    )
    _compose(monkeypatch)
    ctx = _ctx(client, "chatsec")
    session = _session(client, ctx)
    _put_protocol(client, ctx, session["id"])

    body = _chat(client, ctx, session["id"], "увеличь дозу").json()
    assert body["proposal"] is None


def test_chat_tool_loop_calls_draft_once(client, monkeypatch):
    _kb(monkeypatch, [CHUNK])
    _draft_llm(monkeypatch)
    ctx = _ctx(client, "chatloop")
    session, _ = _transcribed(client, ctx, monkeypatch)
    _chat_llm(
        monkeypatch,
        content=json.dumps({"answer": "черновик готов", "proposal": None}),
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "draft_session_protocol",
                    "arguments": json.dumps({"session_id": session["id"]}),
                },
            }
        ],
    )
    _compose(monkeypatch)

    resp = _chat(client, ctx, session["id"], "собери черновик заново")
    assert resp.status_code == 200, resp.text
    protocol = client.get(f"{BASE}/sessions/{session['id']}/protocol", headers=ctx["doctor"])
    assert protocol.status_code == 200


def test_chat_tool_rejects_foreign_session_id(client, monkeypatch, aggregate=None):
    _kb(monkeypatch, [CHUNK])
    _draft_llm(monkeypatch)
    ctx = _ctx(client, "chatforeign")
    mine, _ = _transcribed(client, ctx, monkeypatch)
    other, _ = _transcribed(client, ctx, monkeypatch)
    state = _chat_llm(
        monkeypatch,
        content=json.dumps({"answer": "нет", "proposal": None}),
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "draft_session_protocol",
                    "arguments": json.dumps({"session_id": other["id"]}),
                },
            }
        ],
    )
    _compose(monkeypatch)

    resp = _chat(client, ctx, mine["id"], "собери черновик соседней сессии")
    assert resp.status_code == 200, resp.text
    # Модель увидела отказ тула, а черновик чужой сессии не создан.
    tool_payload = json.dumps(state["payloads"][-1]["messages"], ensure_ascii=False)
    assert "SESSION_NOT_FOUND" in tool_payload
    foreign = client.get(f"{BASE}/sessions/{other['id']}/protocol", headers=ctx["doctor"])
    assert foreign.status_code == 404


def test_chat_masks_pii_before_llm(client, monkeypatch, db_session):
    _kb(monkeypatch, [CHUNK])
    state = _chat_llm(
        monkeypatch, content=json.dumps({"answer": "ok", "proposal": None})
    )
    _compose(monkeypatch)
    ctx = _ctx(client, "chatpii")
    _fio(db_session, ctx)
    session = _session(client, ctx)

    message = f"{PATIENT_FIO} жалуется на тревогу, телефон {PATIENT_PHONE}"
    resp = _chat(client, ctx, session["id"], message)
    assert resp.status_code == 200, resp.text

    sent = json.dumps(state["payloads"], ensure_ascii=False)
    assert PATIENT_FIO not in sent
    assert PATIENT_PHONE not in sent
    assert CLINICIAN_FIO not in sent
    assert "[ПАЦИЕНТ]" in sent


def test_chat_stores_messages_encrypted(client, monkeypatch, db_session):
    _kb(monkeypatch, [CHUNK])
    _chat_llm(monkeypatch, content=json.dumps({"answer": "ответ", "proposal": None}))
    _compose(monkeypatch)
    ctx = _ctx(client, "chatenc")
    session = _session(client, ctx)

    _chat(client, ctx, session["id"], "секретная жалоба пациента")
    db_session.expire_all()
    raw = db_session.execute(
        text("select content_cipher from session_chat_messages where session_id = :i "
             "order by created_at"),
        {"i": session["id"]},
    ).scalars().all()
    assert len(raw) == 2
    for blob in raw:  # в покое — байты pgcrypto, не текст
        assert isinstance(blob, (bytes, memoryview))
        assert b"\xd1\x81\xd0\xb5\xd0\xba\xd1\x80\xd0\xb5\xd1\x82" not in bytes(blob)


def test_chat_limits_message_length(client, monkeypatch):
    _kb(monkeypatch, [CHUNK])
    ctx = _ctx(client, "chatlen")
    session = _session(client, ctx)
    monkeypatch.setattr(settings, "session_chat_message_chars", 10)

    resp = _chat(client, ctx, session["id"], "x" * 11)
    assert resp.status_code == 422
    assert resp.json()["code"] == "MESSAGE_TOO_LONG"


def test_chat_limits_turns(client, monkeypatch):
    _kb(monkeypatch, [CHUNK])
    _chat_llm(monkeypatch, content=json.dumps({"answer": "ok", "proposal": None}))
    _compose(monkeypatch)
    ctx = _ctx(client, "chatturns")
    session = _session(client, ctx)
    monkeypatch.setattr(settings, "session_chat_max_turns", 1)

    assert _chat(client, ctx, session["id"], "первый").status_code == 200
    resp = _chat(client, ctx, session["id"], "второй")
    assert resp.status_code == 409
    assert resp.json()["code"] == "CHAT_LIMIT_REACHED"


def test_chat_gates_and_errors(client, monkeypatch, db_session):
    _kb(monkeypatch, [CHUNK])
    _chat_llm(monkeypatch, content=json.dumps({"answer": "ok", "proposal": None}))
    _compose(monkeypatch)
    ctx = _ctx(client, "chatgates")
    session = _session(client, ctx)

    assert _chat(client, ctx, session["id"], "вопрос", headers=ctx["doctor2"]).status_code == 404
    assert _chat(client, ctx, "00000000-0000-0000-0000-000000000000", "вопрос").status_code == 404

    db_session.execute(
        text("delete from clinician_roster where patient_id = :p"), {"p": ctx["patient_id"]}
    )
    db_session.commit()
    forbidden = _chat(client, ctx, session["id"], "вопрос")
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "NO_ROSTER"

    db_session.execute(
        text("update consents set revoked_at = now() where user_id = :p"),
        {"p": ctx["patient_id"]},
    )
    db_session.execute(
        text("insert into clinician_roster (clinician_id, patient_id) values (:c, :p)"),
        {"c": ctx["clinician_id"], "p": ctx["patient_id"]},
    )
    db_session.commit()
    no_consent = _chat(client, ctx, session["id"], "вопрос")
    assert no_consent.status_code == 403
    assert no_consent.json()["code"] == "CONSENT_REQUIRED"

    monkeypatch.setattr(settings, "assistant_llm_url", "")
    assert _chat(client, ctx, session["id"], "вопрос").status_code == 503
    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    monkeypatch.setattr(settings, "session_ai_enabled", False)
    disabled = _chat(client, ctx, session["id"], "вопрос")
    assert disabled.status_code == 503
    assert disabled.json()["code"] == "SESSION_AI_DISABLED"


def test_chat_allowed_after_session_finished(client, monkeypatch):
    """Обсуждение после приёма допустимо: 409 SESSION_FINISHED чат не блокирует."""
    _kb(monkeypatch, [CHUNK])
    _chat_llm(monkeypatch, content=json.dumps({"answer": "ok", "proposal": None}))
    _compose(monkeypatch)
    ctx = _ctx(client, "chatfin")
    session = _session(client, ctx)
    assert client.post(
        f"{BASE}/sessions/{session['id']}/finish", json={}, headers=ctx["doctor"]
    ).status_code == 200

    resp = _chat(client, ctx, session["id"], "что уточнить на следующем приёме?")
    assert resp.status_code == 200, resp.text


def test_chat_history_requires_access(client):
    ctx = _ctx(client, "chathist")
    session = _session(client, ctx)
    resp = client.get(f"{BASE}/sessions/{session['id']}/chat", headers=ctx["doctor2"])
    assert resp.status_code == 404


# --- ревью PR #100: гонки писателей, история, сбой тула ---------------------


def _protocol_rows(db_session, session_id):
    db_session.expire_all()
    return db_session.execute(
        text("select version, status from session_protocols where session_id = :i order by version"),
        {"i": session_id},
    ).all()


def test_parallel_drafts_share_version_number(client, monkeypatch, db_session):
    """Ревью #100 P1: два черновика одновременно — не 500 и не два активных draft."""
    _kb(monkeypatch)
    _draft_llm(monkeypatch)
    ctx = _ctx(client, "draftrace")
    session, _ = _transcribed(client, ctx, monkeypatch)
    sid, clin = uuid.UUID(session["id"]), uuid.UUID(ctx["clinician_id"])

    async def both():
        async def one():
            async with app_db.AsyncSessionLocal() as session_db:
                try:
                    return await session_ai.draft(session_db, clin, sid)
                except BaseException as exc:  # noqa: BLE001 — ошибка и есть предмет проверки
                    return exc

        return await asyncio.gather(one(), one())

    results = asyncio.run(both())
    assert [r for r in results if isinstance(r, BaseException)] == []
    rows = _protocol_rows(db_session, session["id"])
    assert [r.status for r in rows].count("draft") == 1
    assert rows[-1].status == "draft"


def test_parallel_autodrafts_do_not_report_false_failure(client, monkeypatch, db_session):
    """Ревью #100 P1: коллизия версий в автодрафте — не сбой, `ai_error` пуст."""
    _kb(monkeypatch)
    _draft_llm(monkeypatch)
    ctx = _ctx(client, "autorace")
    session, _ = _transcribed(client, ctx, monkeypatch)
    sid = uuid.UUID(session["id"])

    async def both():
        await asyncio.gather(session_ai._autodraft(sid), session_ai._autodraft(sid))

    asyncio.run(both())
    rows = _protocol_rows(db_session, session["id"])
    assert [r.status for r in rows].count("draft") == 1
    assert _detail(client, ctx["doctor"], session["id"])["ai_error"] is None

    # Повторный автодрафт поверх готового черновика — тихий пропуск, не ошибка.
    asyncio.run(session_ai._autodraft(sid))
    assert _detail(client, ctx["doctor"], session["id"])["ai_error"] is None


def test_chat_survives_unexpected_tool_failure(client, monkeypatch):
    """Ревью #100 P1: исключение в туле не роняет `/chat` (модель видит `error`)."""
    _kb(monkeypatch, [CHUNK])
    ctx = _ctx(client, "chatboom")
    session, _ = _transcribed(client, ctx, monkeypatch)

    def boom(*args, **kwargs):
        raise IntegrityError("insert", {}, Exception("duplicate"))

    monkeypatch.setattr(session_ai, "draft", boom)
    state = _chat_llm(
        monkeypatch,
        content=json.dumps({"answer": "черновик сейчас недоступен", "proposal": None}),
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "draft_session_protocol",
                    "arguments": json.dumps({"session_id": session["id"]}),
                },
            }
        ],
    )
    _compose(monkeypatch)

    resp = _chat(client, ctx, session["id"], "собери черновик")
    assert resp.status_code == 200, resp.text
    tool_results = json.dumps(state["payloads"][-1]["messages"], ensure_ascii=False)
    assert "DRAFT_UNAVAILABLE" in tool_results


def test_chat_history_returns_last_rows(client, monkeypatch):
    """Ревью #100 P2: `limit` — это последние реплики, а не первые."""
    _kb(monkeypatch, [CHUNK])
    _chat_llm(monkeypatch, content=json.dumps({"answer": "ответ", "proposal": None}))
    _compose(monkeypatch)
    ctx = _ctx(client, "chatlast")
    session = _session(client, ctx)
    for question in ("первый вопрос", "второй вопрос"):
        assert _chat(client, ctx, session["id"], question).status_code == 200

    full = client.get(f"{BASE}/sessions/{session['id']}/chat", headers=ctx["doctor"]).json()["items"]
    assert [m["role"] for m in full] == ["clinician", "assistant", "clinician", "assistant"]
    tail = client.get(
        f"{BASE}/sessions/{session['id']}/chat",
        params={"limit": 2},
        headers=ctx["doctor"],
    ).json()["items"]
    assert [m["id"] for m in tail] == [m["id"] for m in full[-2:]]
    assert [m["role"] for m in tail] == ["clinician", "assistant"]
