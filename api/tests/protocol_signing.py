"""Общие шаги тестов FR-40 (подпись/PDF протокола): врач + подтверждённый протокол.

Не `test_*`, чтобы pytest не собирал модуль: это фикстуры-хелперы для
`test_protocol_sign.py` и `test_protocol_pdf.py`.
"""

from __future__ import annotations

from asn1crypto import cms

from app.services import artifacts

BASE = "/api/v1/clinician"

FULL = {
    "passport": {
        "visit_at": "2026-10-26T10:00:00+03:00",
        "channel": "in_person",
        "legal_basis": "ИДС № 12 от 2026-05-04",
    },
    "complaints": "тревога, бессонница",
    "anamnesis_morbi": "впервые в 2024 году",
    "mental_status": {
        "appearance_behavior": "опрятен",
        "consciousness_orientation": "ориентирован",
        "perception": "без обманов восприятия",
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


def ctx(client, prefix: str = "sg") -> dict:
    """Врач с пациентом в roster."""
    from .conftest import admin_ctx, clinician_ctx, patient_ctx

    admin, _ = admin_ctx(client, f"{prefix}-admin@test.ru")
    patient, patient_id = patient_ctx(client, f"{prefix}-p1@test.ru")
    doctor, clinician_id = clinician_ctx(client, f"{prefix}-doc@test.ru")
    assert (
        client.post(
            "/api/v1/admin/roster",
            json={"patient_id": patient_id, "clinician_id": clinician_id},
            headers=admin,
        ).status_code
        == 201
    )
    return {
        "patient": patient,
        "patient_id": patient_id,
        "doctor": doctor,
        "clinician_id": clinician_id,
    }


def new_session(client, context: dict) -> dict:
    resp = client.post(
        f"{BASE}/sessions",
        json={"patient_id": context["patient_id"]},
        headers=context["doctor"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def draft(client, context: dict) -> dict:
    """Сессия с черновиком протокола (ещё не подтверждённым)."""
    session = new_session(client, context)
    url = f"{BASE}/sessions/{session['id']}/protocol"
    resp = client.put(
        url, json={"sections": FULL, "diagnosis_code": "F41.1"}, headers=context["doctor"]
    )
    assert resp.status_code == 200, resp.text
    return session


def approved(client, context: dict) -> dict:
    """Сессия с подтверждённым протоколом."""
    session = draft(client, context)
    resp = client.post(
        f"{BASE}/sessions/{session['id']}/protocol/approve",
        json={},
        headers=context["doctor"],
    )
    assert resp.status_code == 200, resp.text
    return session


def artifact_row(db_session, kind: str, protocol_id: str):
    """Последний артефакт вида `kind` у протокола (синхронная сессия теста)."""
    from app.models import DocumentArtifact

    return (
        db_session.query(DocumentArtifact)
        .filter(
            DocumentArtifact.kind == kind,
            DocumentArtifact.protocol_id == protocol_id,
        )
        .order_by(DocumentArtifact.created_at.desc())
        .first()
    )


def signature_rows(db_session, protocol_id: str) -> dict:
    from app.models import ProtocolSignature

    rows = (
        db_session.query(ProtocolSignature)
        .filter(ProtocolSignature.protocol_id == protocol_id)
        .all()
    )
    assert rows, "подписи не найдены"
    return {row.role: row for row in rows}


def cda_bytes(db_session, protocol_id: str) -> bytes:
    row = artifact_row(db_session, "protocol_cda", protocol_id)
    assert row is not None, "артефакт СЭМД отсутствует"
    return artifacts.get_bytes(row)


def parse_signed_data(der: bytes) -> cms.SignedData:
    info = cms.ContentInfo.load(der)
    assert info["content_type"].native == "signed_data"
    return info["content"]


def replace_cda(db_session, session_id: str, protocol_id: str, data: bytes) -> None:
    """Подменить сохранённый СЭМД (атакующий с доступом к хранилищу)."""
    from app.models import DocumentArtifact

    old = artifact_row(db_session, "protocol_cda", protocol_id)
    fresh = artifacts.put_bytes(
        "protocol_cda",
        data,
        session_id=session_id,
        protocol_id=protocol_id,
        mime="application/xml",
    )
    db_session.add(fresh)
    db_session.delete(old)
    db_session.commit()


def replace_signature(
    db_session, row, *, cert_der: bytes, cms_der: bytes, protocol_id: str, session_id: str
) -> None:
    """Переписать подпись: чужой сертификат и/или чужой CMS-контейнер."""
    fresh = artifacts.put_bytes(
        "signature",
        cms_der,
        session_id=session_id,
        protocol_id=protocol_id,
        mime="application/pkcs7-signature",
    )
    db_session.add(fresh)
    db_session.flush()
    row.cert_der = cert_der
    row.cms_artifact_id = fresh.id
    db_session.add(row)
    db_session.commit()


__all__ = [
    "BASE",
    "FULL",
    "approved",
    "artifact_row",
    "cda_bytes",
    "ctx",
    "draft",
    "new_session",
    "parse_signed_data",
    "replace_cda",
    "replace_signature",
    "signature_rows",
]
