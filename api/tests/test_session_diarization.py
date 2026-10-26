"""FR-42 (docs/18 §3.5, §4.5): роли реплик «врач / пациент» в расшифровке.

Сайдкар подменяется `ml_client.transcribe_file`, LLM — `llm_router.chat_completion`:
проверяем реальный Postgres, шифрованный артефакт и HTTP-гейты, без сети и GPU.

Покрывается: роли по умолчанию (первый заговоривший — врач, `assumed=true`),
пустые роли при трёх кластерах, честная деградация без диаризации, PATCH
(перестановка, `assumed=false`, аудит, перезапись артефакта), 422 на дубли и
несуществующие метки, 404 чужой сессии, 403 без согласия, роли в тексте для LLM.
"""

from __future__ import annotations

import copy
import json
import time

import pytest
from sqlalchemy import text

from app.config import settings
from app.models import DocumentArtifact
from app.services import artifacts as artifacts_svc
from app.services import llm_router, ml_client

BASE = "/api/v1/clinician"

AUDIO = b"OggS" + b"\x00" * 512

LLM_JSON = json.dumps({"complaints": "тревога", "mental_status": {}})


def _ml_result(*, speakers: list[str | None], diarized: bool = True) -> dict:
    """Ответ сайдкара: текст реплик + метки кластеров + флаги диаризации."""
    texts = [
        "Здравствуйте, на что жалуетесь?",
        "Болит голова две недели.",
        "Принимали что-нибудь?",
    ]
    segments = [
        {"start": float(index * 2), "end": float(index * 2 + 2),
         "speaker": speakers[index], "text": texts[index]}
        for index in range(len(texts))
    ]
    counts: dict[str, float] = {}
    for segment in segments:
        if segment["speaker"] is not None:
            counts[segment["speaker"]] = counts.get(segment["speaker"], 0.0) + 2.0
    return {
        "model": "stub-asr",
        "duration_s": 6.0,
        "language": "ru",
        "segments": segments,
        "speakers": [
            {"id": label, "label": None, "seconds": seconds, "segments": 1}
            for label, seconds in counts.items()
        ],
        "diarized": diarized,
        "degraded": not diarized,
    }


TWO_SPEAKERS = _ml_result(speakers=["A", "B", "A"])
THREE_SPEAKERS = _ml_result(speakers=["A", "B", "C"])
DEGRADED = _ml_result(speakers=[None, None, None], diarized=False)


# --- фикстуры и помощники ---------------------------------------------------


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "session_ai_enabled", True)
    monkeypatch.setattr(settings, "assistant_llm_url", "http://llm:18080/v1")
    monkeypatch.setattr(settings, "prod_mode", False)
    monkeypatch.setattr(settings, "depersonalize_ner", "off")
    monkeypatch.setattr(settings, "session_audio_max_mb", 200)
    # FR-44: автодрафт тестируется отдельно и здесь только мешал бы ролям.
    monkeypatch.setattr(settings, "session_ai_autodraft", False)


def _ctx(client, tag: str = "diar") -> dict:
    """Врач с пациентом в roster и второй врач без доступа к этому пациенту."""
    from .conftest import admin_ctx, clinician_ctx, patient_ctx

    admin, _ = admin_ctx(client, f"diar-admin{tag}@test.ru")
    hp, pid = patient_ctx(client, f"diar-p{tag}@test.ru")
    h_doc, cid = clinician_ctx(client, f"diar-doc{tag}@test.ru")
    h_doc2, _ = clinician_ctx(client, f"diar-doc2{tag}@test.ru")
    resp = client.post(
        "/api/v1/admin/roster",
        json={"patient_id": pid, "clinician_id": cid},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return {"patient": hp, "patient_id": pid, "doctor": h_doc, "doctor2": h_doc2,
            "admin": admin, "clinician_id": cid}


def _fake_ml(monkeypatch, payload: dict, state: dict | None = None) -> dict:
    state = {} if state is None else state
    state.setdefault("calls", 0)

    def fake(path, *, filename="audio.bin", mime="application/octet-stream",
             language="ru", diarize=True):
        state["calls"] += 1
        state["diarize"] = diarize
        return copy.deepcopy(payload)

    monkeypatch.setattr(ml_client, "transcribe_file", fake)
    return state


def _fake_llm(monkeypatch) -> dict:
    captured: dict = {"messages": []}

    def fake(messages, *, temperature=0.0, timeout=None):
        captured["messages"].append(messages)
        return {"choices": [{"message": {"role": "assistant", "content": LLM_JSON}}]}

    monkeypatch.setattr(llm_router, "chat_completion", fake)
    return captured


def _detail(client, headers, session_id: str) -> dict:
    resp = client.get(f"{BASE}/sessions/{session_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _wait_ai_status(client, headers, session_id: str, want: set[str]) -> dict:
    deadline = time.time() + 15.0
    last: dict = {}
    while time.time() < deadline:
        last = _detail(client, headers, session_id)
        if last["ai_status"] in want:
            return last
        time.sleep(0.02)
    raise AssertionError(f"ai_status={last.get('ai_status')!r}, ждали {want}")


def _transcribed(
    client, ctx, monkeypatch, payload: dict, state: dict | None = None
) -> str:
    """Сессия с готовой расшифровкой; возвращает id сессии."""
    _fake_ml(monkeypatch, payload, state)
    resp = client.post(
        f"{BASE}/sessions",
        json={"patient_id": ctx["patient_id"], "audio_consent": True},
        headers=ctx["doctor"],
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    upload = client.post(
        f"{BASE}/sessions/{session_id}/audio",
        files={"file": ("visit.webm", AUDIO, "audio/webm")},
        headers=ctx["doctor"],
    )
    assert upload.status_code == 201, upload.text
    started = client.post(
        f"{BASE}/sessions/{session_id}/transcribe", headers=ctx["doctor"]
    )
    assert started.status_code == 202, started.text
    _wait_ai_status(client, ctx["doctor"], session_id, {"transcribed"})
    return session_id


def _transcript(client, ctx, session_id: str) -> dict:
    resp = client.get(f"{BASE}/sessions/{session_id}/transcript", headers=ctx["doctor"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def _row(db_session, session_id: str):
    return db_session.execute(
        text(
            "select diarized, speaker_roles, speaker_roles_assumed, segments_artifact_id "
            "from session_transcripts where session_id = cast(:s as uuid)"
        ).bindparams(s=session_id)
    ).one()


def _artifact_segments(db_session, session_id: str) -> list[dict]:
    """Расшифровка «в покое»: читаем через сервис артефактов (AES-GCM)."""
    artifact_id = _row(db_session, session_id).segments_artifact_id
    artifact = db_session.get(DocumentArtifact, artifact_id)
    assert artifact is not None
    return json.loads(artifacts_svc.get_bytes(artifact).decode("utf-8"))


def _patch(client, ctx, session_id: str, roles: dict, *, headers=None):
    return client.patch(
        f"{BASE}/sessions/{session_id}/transcript/speakers",
        json={"roles": roles},
        headers=headers or ctx["doctor"],
    )


# --- роли по умолчанию ------------------------------------------------------


def test_first_speaker_is_clinician_by_default(client, monkeypatch, db_session):
    """Первый заговоривший — врач: роли назначены, но помечены как допущение."""
    ctx = _ctx(client)
    session_id = _transcribed(client, ctx, monkeypatch, TWO_SPEAKERS)

    payload = _transcript(client, ctx, session_id)
    assert payload["diarized"] is True
    assert payload["speaker_roles"] == {"A": "clinician", "B": "patient"}
    assert payload["speaker_roles_assumed"] is True
    assert [(item["speaker"], item["role"]) for item in payload["segments"]] == [
        ("A", "clinician"),
        ("B", "patient"),
        ("A", "clinician"),
    ]

    row = _row(db_session, session_id)
    assert row.diarized is True
    assert row.speaker_roles == {"A": "clinician", "B": "patient"}
    assert row.speaker_roles_assumed is True
    # Роли лежат и в артефакте: это единственный источник текста расшифровки.
    assert [segment["role"] for segment in _artifact_segments(db_session, session_id)] == [
        "clinician",
        "patient",
        "clinician",
    ]


def test_three_clusters_leave_roles_empty(client, monkeypatch, db_session):
    """Три голоса — роли не угадываем: врач увидит «Говорящий A/B/C»."""
    ctx = _ctx(client)
    session_id = _transcribed(client, ctx, monkeypatch, THREE_SPEAKERS)

    payload = _transcript(client, ctx, session_id)
    assert payload["diarized"] is True
    assert payload["speaker_roles"] == {}
    assert payload["speaker_roles_assumed"] is False
    assert [item["speaker"] for item in payload["segments"]] == ["A", "B", "C"]
    assert all(item["role"] is None for item in payload["segments"])
    assert _row(db_session, session_id).speaker_roles is None


def test_degraded_diarization_is_honest(client, monkeypatch, db_session):
    """Сайдкар не разделил говорящих — 200, но ролей и меток нет."""
    ctx = _ctx(client)
    session_id = _transcribed(client, ctx, monkeypatch, DEGRADED)

    payload = _transcript(client, ctx, session_id)
    assert payload["diarized"] is False
    assert payload["speaker_roles"] == {}
    assert payload["speaker_roles_assumed"] is False
    assert all(item["speaker"] is None and item["role"] is None
               for item in payload["segments"])
    row = _row(db_session, session_id)
    assert row.diarized is False and row.speaker_roles is None


def test_llm_text_uses_russian_roles(client, monkeypatch):
    """В промпт уходят «Врач:/Пациент:», а не латинские метки кластеров."""
    ctx = _ctx(client)
    llm = _fake_llm(monkeypatch)
    session_id = _transcribed(client, ctx, monkeypatch, TWO_SPEAKERS)

    resp = client.post(
        f"{BASE}/sessions/{session_id}/protocol/draft", headers=ctx["doctor"]
    )
    assert resp.status_code == 201, resp.text
    prompt = llm["messages"][0][-1]["content"]
    assert "Врач: Здравствуйте, на что жалуетесь?" in prompt
    assert "Пациент: Болит голова две недели." in prompt
    assert "A:" not in prompt and "B:" not in prompt


def test_llm_text_without_diarization_says_реплика(client, monkeypatch):
    ctx = _ctx(client)
    llm = _fake_llm(monkeypatch)
    session_id = _transcribed(client, ctx, monkeypatch, DEGRADED)

    assert client.post(
        f"{BASE}/sessions/{session_id}/protocol/draft", headers=ctx["doctor"]
    ).status_code == 201
    prompt = llm["messages"][0][-1]["content"]
    assert "реплика: Здравствуйте" in prompt


# --- PATCH: роли подтверждает врач ------------------------------------------


def test_patch_swaps_roles_and_clears_assumed(client, monkeypatch, db_session):
    ctx = _ctx(client)
    session_id = _transcribed(client, ctx, monkeypatch, TWO_SPEAKERS)

    resp = _patch(client, ctx, session_id, {"A": "patient", "B": "clinician"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["speaker_roles"] == {"A": "patient", "B": "clinician"}
    assert body["speaker_roles_assumed"] is False
    assert [(item["speaker"], item["role"]) for item in body["segments"]] == [
        ("A", "patient"),
        ("B", "clinician"),
        ("A", "patient"),
    ]

    # Роли доехали до БД и до артефакта — и GET отдаёт то же самое.
    row = _row(db_session, session_id)
    assert row.speaker_roles == {"A": "patient", "B": "clinician"}
    assert row.speaker_roles_assumed is False
    assert [s["role"] for s in _artifact_segments(db_session, session_id)] == [
        "patient",
        "clinician",
        "patient",
    ]
    again = _transcript(client, ctx, session_id)
    assert again["speaker_roles"] == {"A": "patient", "B": "clinician"}

    # Текст не изменился: PATCH правит только роли.
    assert again["mask_categories"] == _transcript(client, ctx, session_id)["mask_categories"]
    assert [s["text"] for s in again["segments"]] == [
        "Здравствуйте, на что жалуетесь?",
        "Болит голова две недели.",
        "Принимали что-нибудь?",
    ]

    # Аудит: только id сессии, без текста и без самих ролей (§2).
    audited = db_session.execute(
        text(
            "select actor_id, object_type, object_id, ip from audit_log "
            "where action = 'SESSION_TRANSCRIPT_SPEAKERS' and object_id = :s"
        ).bindparams(s=session_id)
    ).all()
    assert len(audited) == 1
    assert audited[0].object_type == "clinical_session"
    assert str(audited[0].actor_id) == ctx["clinician_id"]


def test_patch_changes_llm_text(client, monkeypatch):
    """После перестановки ролей черновик опирается на новые роли."""
    ctx = _ctx(client)
    llm = _fake_llm(monkeypatch)
    session_id = _transcribed(client, ctx, monkeypatch, TWO_SPEAKERS)
    assert _patch(client, ctx, session_id, {"A": "patient", "B": "clinician"}).status_code == 200

    assert client.post(
        f"{BASE}/sessions/{session_id}/protocol/draft", headers=ctx["doctor"]
    ).status_code == 201
    prompt = llm["messages"][0][-1]["content"]
    assert "Пациент: Здравствуйте, на что жалуетесь?" in prompt
    assert "Врач: Болит голова две недели." in prompt


def test_patch_partial_roles_allowed(client, monkeypatch):
    """Одну метку можно пометить `unknown` — вторая остаётся как была."""
    ctx = _ctx(client)
    session_id = _transcribed(client, ctx, monkeypatch, TWO_SPEAKERS)

    resp = _patch(client, ctx, session_id, {"A": "unknown"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["speaker_roles"] == {"A": "unknown"}
    # `unknown` — явное «не знаю»: метка в UI станет «Говорящий A», а роль B
    # остаётся неизвестной, потому что вторая метка не пришла.
    assert [item["role"] for item in body["segments"]] == ["unknown", None, "unknown"]


def test_patch_duplicate_roles_422(client, monkeypatch):
    ctx = _ctx(client)
    session_id = _transcribed(client, ctx, monkeypatch, TWO_SPEAKERS)

    resp = _patch(client, ctx, session_id, {"A": "clinician", "B": "clinician"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "SPEAKER_ROLE_DUPLICATE"


def test_patch_invalid_role_422(client, monkeypatch):
    ctx = _ctx(client)
    session_id = _transcribed(client, ctx, monkeypatch, TWO_SPEAKERS)

    resp = _patch(client, ctx, session_id, {"A": "doctor", "B": "patient"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "VALIDATION"


def test_patch_unknown_label_422(client, monkeypatch):
    """Метки, которой нет в расшифровке, — 422, а не молчаливая запись."""
    ctx = _ctx(client)
    session_id = _transcribed(client, ctx, monkeypatch, TWO_SPEAKERS)

    resp = _patch(client, ctx, session_id, {"C": "clinician"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "SPEAKER_UNKNOWN"


def test_patch_foreign_session_404(client, monkeypatch):
    """Чужая сессия неотличима от несуществующей."""
    ctx = _ctx(client, "foreign")
    session_id = _transcribed(client, ctx, monkeypatch, TWO_SPEAKERS)

    resp = _patch(
        client, ctx, session_id, {"A": "clinician", "B": "patient"},
        headers=ctx["doctor2"],
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


def test_patch_without_consent_403(client, monkeypatch, db_session):
    """Согласие отозвано — роли не правим, даже если врач их назначил."""
    ctx = _ctx(client, "nocon")
    session_id = _transcribed(client, ctx, monkeypatch, TWO_SPEAKERS)
    db_session.execute(
        text("update consents set revoked_at = now() where user_id = cast(:u as uuid)")
        .bindparams(u=ctx["patient_id"])
    )
    db_session.commit()

    resp = _patch(client, ctx, session_id, {"A": "clinician", "B": "patient"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "CONSENT_REQUIRED"


def test_patch_without_transcript_404(client, monkeypatch):
    ctx = _ctx(client, "notrans")
    resp = client.post(
        f"{BASE}/sessions",
        json={"patient_id": ctx["patient_id"], "audio_consent": True},
        headers=ctx["doctor"],
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    resp = _patch(client, ctx, session_id, {"A": "clinician", "B": "patient"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "TRANSCRIPT_MISSING"


def test_transcribe_asks_sidecar_for_diarization(client, monkeypatch):
    """api всегда просит диаризацию: гейт — на стороне сайдкара (CC_ML_DIARIZE)."""
    ctx = _ctx(client, "flag")
    state: dict = {}
    _transcribed(client, ctx, monkeypatch, TWO_SPEAKERS, state)
    assert state["calls"] == 1
    assert state["diarize"] is True
