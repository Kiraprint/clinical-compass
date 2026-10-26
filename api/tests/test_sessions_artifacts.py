"""FR-38…41: артефакты сессий — AES-256-GCM на диске (docs/18 §3.6, §4.6).

Проверяется формат файла (`CCART1` + nonce + ciphertext), отсутствие plaintext на
диске и в сырых байтах БД, хэш шифротекста, обнаружение подмены (ошибка, а не
молчаливый мусор) и учёт артефактов в `GET /clinician/sessions/{id}`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import settings
from app.errors import Problem
from app.services import artifacts as svc

BASE = "/api/v1/clinician"
SECRET = "пациент жалуется на тревогу и бессонницу"


def _plain() -> bytes:
    return json.dumps({"segments": [{"text": SECRET}]}, ensure_ascii=False).encode()


def _file(artifact) -> Path:
    return Path(settings.artifacts_dir) / artifact.path


def test_roundtrip_format_and_no_plaintext_on_disk():
    assert not str(settings.artifacts_dir).startswith("/var/lib")  # conftest: tmp-каталог
    data = _plain()
    artifact = svc.put_bytes("transcript", data, session_id=None, protocol_id=None, mime="application/json")

    assert artifact.kind == "transcript"
    assert artifact.encrypted is True
    assert artifact.path.startswith("transcript/") or artifact.path.startswith("transcript\\")
    assert artifact.path.endswith(".bin")
    assert "жалуется" not in artifact.path  # без ПДн в имени

    blob = _file(artifact).read_bytes()
    assert blob.startswith(b"CCART1")
    assert len(blob) == len(b"CCART1") + 12 + len(data) + 16  # заголовок + nonce + ct + tag
    assert data not in blob  # на диске только шифротекст
    assert SECRET.encode() not in blob
    assert artifact.size_bytes == len(blob)
    assert artifact.sha256 == hashlib.sha256(blob).hexdigest()  # хэш шифротекста

    assert svc.get_bytes(artifact) == data


def test_tampered_file_is_detected():
    artifact = svc.put_bytes("audio", b"RIFF....fake-wav", mime="audio/wav")
    path = _file(artifact)
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0x01  # подмена одного байта шифротекста
    path.write_bytes(bytes(blob))

    with pytest.raises(Problem) as exc:
        svc.get_bytes(artifact)
    assert exc.value.status == 500
    assert exc.value.code in ("ARTIFACT_CORRUPT",)  # ошибка, а не «как будто ок»


def test_unknown_magic_is_rejected():
    artifact = svc.put_bytes("signature", b"detached", mime="application/pkcs7-signature")
    path = _file(artifact)
    path.write_bytes(b"NOTCC1" + path.read_bytes()[6:])

    with pytest.raises(Problem) as exc:
        svc.get_bytes(artifact)
    assert exc.value.code == "ARTIFACT_CORRUPT"


def test_unknown_kind_and_bad_payload_rejected():
    with pytest.raises(Problem) as exc:
        svc.put_bytes("diary", b"x", mime="text/plain")
    assert exc.value.status == 422 and exc.value.code == "ARTIFACT_KIND_UNKNOWN"

    with pytest.raises(Problem) as exc:
        svc.put_bytes("audio", "строка вместо байтов", mime="audio/wav")  # type: ignore[arg-type]
    assert exc.value.code == "ARTIFACT_DATA_INVALID"


def test_path_outside_root_rejected():
    artifact = svc.put_bytes("audio", b"bytes", mime="audio/wav")
    artifact.path = "../outside.bin"
    with pytest.raises(Problem) as exc:
        svc.get_bytes(artifact)
    assert exc.value.code == "ARTIFACT_CORRUPT"


def test_missing_file_reported():
    artifact = svc.put_bytes("transcript", b"{}", mime="application/json")
    _file(artifact).unlink()
    with pytest.raises(Problem) as exc:
        svc.get_bytes(artifact)
    assert exc.value.code == "ARTIFACT_MISSING"


def test_remove_deletes_file():
    artifact = svc.put_bytes("audio", b"wave", mime="audio/wav")
    path = _file(artifact)
    assert path.exists()
    svc.remove(artifact)
    assert not path.exists()
    svc.remove(artifact)  # идемпотентно


def test_session_detail_lists_artifacts(client, db_session):
    """Артефакт, привязанный к сессии, виден врачу в SessionDetail (без файла)."""
    from app.models import DocumentArtifact

    from .conftest import admin_ctx, clinician_ctx, patient_ctx

    admin, _ = admin_ctx(client, "art-admin@test.ru")
    _hp, pid = patient_ctx(client, "art-p1@test.ru")
    h_doc, cid = clinician_ctx(client, "art-doc@test.ru")
    assert (
        client.post(
            "/api/v1/admin/roster",
            json={"patient_id": pid, "clinician_id": cid},
            headers=admin,
        ).status_code
        == 201
    )
    session = client.post(f"{BASE}/sessions", json={"patient_id": pid}, headers=h_doc).json()

    import uuid

    artifact = svc.put_bytes(
        "audio",
        b"wav-bytes-" + SECRET.encode(),
        session_id=uuid.UUID(session["id"]),
        protocol_id=None,
        mime="audio/wav",
    )
    db_session.add(artifact)
    db_session.commit()

    detail = client.get(f"{BASE}/sessions/{session['id']}", headers=h_doc)
    assert detail.status_code == 200, detail.text
    items = detail.json()["artifacts"]
    assert len(items) == 1
    assert items[0]["kind"] == "audio" and items[0]["mime"] == "audio/wav"
    assert items[0]["sha256"] == artifact.sha256
    assert items[0]["size_bytes"] == artifact.size_bytes

    # В БД нет ни текста, ни ключей: только метаданные и относительный путь.
    row = db_session.execute(
        text(
            "SELECT path, sha256, size_bytes FROM document_artifacts WHERE session_id = CAST(:s AS uuid)"
        ).bindparams(s=session["id"])
    ).one()
    assert "жалуется" not in row.path
    assert row.sha256 == hashlib.sha256(_file(artifact).read_bytes()).hexdigest()
    stored = db_session.execute(
        text("SELECT string_agg(concat_ws(' ', path, sha256, mime), ' ') FROM document_artifacts")
    ).scalar_one()
    assert SECRET not in stored

    # Расшифровка через сервис — исходные байты.
    loaded = db_session.get(DocumentArtifact, artifact.id)
    assert svc.get_bytes(loaded).startswith(b"wav-bytes-")
