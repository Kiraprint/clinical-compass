"""FR-40 (docs/18 §4.5, §7): подпись протокола — демо-УКЭП, две подписи, verify.

Контракт проверяется на живом Postgres через TestClient: состояние протокола,
отсоединённый CMS (пустой eContent + Стрибог-хэш), привязка подписантов, аудит и
поведение verify на подменённом документе, просроченном и «чужом» сертификате.
"""

from __future__ import annotations

from sqlalchemy import text

from app.config import settings
from app.models import DocumentArtifact
from app.services import artifacts
from app.signing import ca, cms, gost
from app.signing import x509 as signing_x509

from . import protocol_signing as ps
from .protocol_signing import BASE

GOST_KEY_OID = "1.2.643.7.1.1.1.1"
GOST_DIGEST_OID = "1.2.643.7.1.1.2.2"
GOST_SIGN_OID = "1.2.643.7.1.1.3.2"


def _sign(client, context, session):
    return client.post(
        f"{BASE}/sessions/{session['id']}/protocol/sign", headers=context["doctor"]
    )


def _verify(client, context, session):
    return client.post(
        f"{BASE}/sessions/{session['id']}/protocol/verify", headers=context["doctor"]
    )


def cms_artifact(db_session, row) -> DocumentArtifact:
    """Артефакт CMS-подписи по строке protocol_signatures."""
    return db_session.get(DocumentArtifact, row.cms_artifact_id)


def test_sign_requires_approved(client):
    context = ps.ctx(client, "sg-draft")
    session = ps.draft(client, context)

    resp = _sign(client, context, session)
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "PROTOCOL_NOT_APPROVED"
    # Отказ не оставляет полусостояния: черновик остаётся черновиком, подписей нет.
    detail = client.get(f"{BASE}/sessions/{session['id']}", headers=context["doctor"])
    assert detail.json()["protocol"]["status"] == "draft"
    assert detail.json()["signature"] is None


def test_sign_creates_two_signatures(client, db_session):
    context = ps.ctx(client, "sg-mo")
    session = ps.approved(client, context)

    resp = _sign(client, context, session)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "signed"
    signature = payload["signature"]
    assert signature["status"] == "signed"
    assert {item["role"] for item in signature["signatures"]} == {"clinician", "mo"}

    rows = ps.signature_rows(db_session, payload["id"])
    clinician, mo = rows["clinician"], rows["mo"]
    # Разные сертификаты и разные подписанты: врач сессии и настроенное лицо МО.
    assert clinician.cert_serial != mo.cert_serial
    assert clinician.cert_thumbprint != mo.cert_thumbprint
    assert clinician.signer_user_id is not None
    assert mo.signer_user_id is None
    assert mo.fio == settings.signing_mo_fio
    assert mo.position == settings.signing_mo_position
    assert clinician.content_sha256 == mo.content_sha256

    detail = client.get(f"{BASE}/sessions/{session['id']}", headers=context["doctor"])
    assert detail.status_code == 200
    assert detail.json()["signature"]["status"] == "signed"
    assert len(detail.json()["signature"]["signatures"]) == 2


def test_sign_twice_is_conflict(client):
    context = ps.ctx(client, "sg-twice")
    session = ps.approved(client, context)

    assert _sign(client, context, session).status_code == 200
    again = _sign(client, context, session)
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "PROTOCOL_ALREADY_SIGNED"


def test_cms_is_detached_and_digest_matches_cda(client, db_session):
    context = ps.ctx(client, "sg-cms")
    session = ps.approved(client, context)
    signed = _sign(client, context, session)
    assert signed.status_code == 200, signed.text
    protocol_id = signed.json()["id"]

    cda = ps.cda_bytes(db_session, protocol_id)
    rows = ps.signature_rows(db_session, protocol_id)
    for role in ("clinician", "mo"):
        row = rows[role]
        signed_data = ps.parse_signed_data(artifacts.get_bytes(cms_artifact(db_session, row)))
        # Отсоединённая подпись: сам документ в контейнер не вложен.
        assert signed_data["encap_content_info"]["content"].native is None
        signer_info = signed_data["signer_infos"][0]
        assert signer_info["digest_algorithm"]["algorithm"].dotted == GOST_DIGEST_OID
        assert signer_info["signature_algorithm"]["algorithm"].dotted == GOST_SIGN_OID
        digest = [
            attribute["values"][0].native
            for attribute in signer_info["signed_attrs"]
            if attribute["type"].native == "message_digest"
        ][0]
        assert digest == gost.digest(cda)
        assert row.content_sha256 == gost.digest(cda).hex()
        # Сертификат подписи — ГОСТ-ключ, выпущенный демо-УЦ.
        certificate = signed_data["certificates"][0].chosen
        spki = certificate["tbs_certificate"]["subject_public_key_info"]
        assert spki["algorithm"]["algorithm"].dotted == GOST_KEY_OID


def test_verify_valid(client):
    context = ps.ctx(client, "sg-valid")
    session = ps.approved(client, context)
    assert _sign(client, context, session).status_code == 200

    resp = _verify(client, context, session)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "valid", payload
    assert payload["reason"] is None
    assert {item["status"] for item in payload["signatures"]} == {"valid"}
    assert payload["document"]["digest"]
    assert payload["document"]["digest_algorithm"] == "GOST3411-2012-256"


def test_tampered_cda_is_digest_mismatch(client, db_session):
    context = ps.ctx(client, "sg-tamper")
    session = ps.approved(client, context)
    signed = _sign(client, context, session)
    assert signed.status_code == 200, signed.text
    protocol_id = signed.json()["id"]

    original = ps.cda_bytes(db_session, protocol_id)
    tampered = bytearray(original)
    tampered[original.index("тревога".encode())] ^= 0x01  # один байт в тексте СЭМД
    ps.replace_cda(db_session, session["id"], protocol_id, bytes(tampered))

    resp = _verify(client, context, session)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "digest_mismatch"


def test_foreign_ca_is_unknown_ca(client, db_session, tmp_path):
    context = ps.ctx(client, "sg-foreign")
    session = ps.approved(client, context)
    signed = _sign(client, context, session)
    assert signed.status_code == 200, signed.text
    protocol_id = signed.json()["id"]

    cda = ps.cda_bytes(db_session, protocol_id)
    foreign = ca.authority(tmp_path / "foreign-ca").issue("mo", ca.mo_identity(), 365)
    rows = ps.signature_rows(db_session, protocol_id)
    ps.replace_signature(
        db_session,
        rows["mo"],
        cert_der=foreign.der,
        cms_der=cms.sign(cda, foreign),
        protocol_id=protocol_id,
        session_id=session["id"],
    )

    resp = _verify(client, context, session)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "unknown_ca", payload
    statuses = {item["role"]: item["status"] for item in payload["signatures"]}
    assert statuses["mo"] == "unknown_ca"
    assert statuses["clinician"] == "valid"


def test_expired_certificate(client, monkeypatch):
    context = ps.ctx(client, "sg-expired")
    session = ps.approved(client, context)
    monkeypatch.setattr(settings, "signing_cert_days", -1)

    signed = _sign(client, context, session)
    assert signed.status_code == 200, signed.text

    resp = _verify(client, context, session)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "expired", resp.json()


def test_sign_audit_row(client, db_session):
    context = ps.ctx(client, "sg-audit")
    session = ps.approved(client, context)
    assert _sign(client, context, session).status_code == 200

    rows = db_session.execute(
        text(
            "select action, object_type, object_id from audit_log "
            "where action = 'SESSION_PROTOCOL_SIGN'"
        )
    ).all()
    assert len(rows) == 1
    action, object_type, object_id = rows[0]
    assert action == "SESSION_PROTOCOL_SIGN"
    assert object_type == "session_protocol"
    assert object_id == f"{session['id']}:v1"


def test_sign_requires_clinician(client):
    context = ps.ctx(client, "sg-gate")
    session = ps.approved(client, context)

    resp = client.post(
        f"{BASE}/sessions/{session['id']}/protocol/sign", headers=context["patient"]
    )
    assert resp.status_code == 403


def test_verify_after_edit_uses_signed_version(client):
    """Правка после подписи создаёт v2, но подписанный v1 остаётся проверяемым."""
    context = ps.ctx(client, "sg-superseded")
    session = ps.approved(client, context)
    assert _sign(client, context, session).status_code == 200

    url = f"{BASE}/sessions/{session['id']}/protocol"
    edited = dict(ps.FULL)
    edited["recommendations"] = "КПТ, контроль через неделю"
    resp = client.put(url, json={"sections": edited}, headers=context["doctor"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    assert resp.json()["status"] == "draft"

    verified = _verify(client, context, session)
    assert verified.status_code == 200, verified.text
    payload = verified.json()
    assert payload["status"] == "valid", payload
    assert payload["document"]["version"] == 1


def test_foreign_cms_with_genuine_cert_der_is_invalid(client, db_session, tmp_path):
    """Подпись чужого УЦ при нетронутой строке подписи не проходит.

    Регресс на обход привязки: цепочка проверялась по `row.cert_der`, а подпись —
    по сертификату внутри CMS; без сверки этих двух сертификатов подпись,
    сделанная посторонним УЦ, принималась как валидная.
    """
    context = ps.ctx(client, "sg-foreign-cms")
    session = ps.approved(client, context)
    signed = _sign(client, context, session)
    assert signed.status_code == 200, signed.text
    protocol_id = signed.json()["id"]

    cda = ps.cda_bytes(db_session, protocol_id)
    rows = ps.signature_rows(db_session, protocol_id)
    foreign = ca.authority(tmp_path / "foreign-ca").issue("mo", ca.mo_identity(), 365)
    ps.replace_signature(
        db_session,
        rows["mo"],
        cert_der=rows["mo"].cert_der,  # строка подписи не тронута
        cms_der=cms.sign(cda, foreign),  # контейнер подписан чужим УЦ
        protocol_id=protocol_id,
        session_id=session["id"],
    )

    payload = _verify(client, context, session).json()
    assert payload["status"] == "invalid", payload
    statuses = {item["role"]: item["status"] for item in payload["signatures"]}
    assert statuses["mo"] == "invalid"
    assert statuses["clinician"] == "valid"


def test_swapped_cms_between_roles_is_invalid(client, db_session):
    """Перенос CMS врача в строку МО (и наоборот) даёт `invalid` у обеих."""
    context = ps.ctx(client, "sg-swap")
    session = ps.approved(client, context)
    signed = _sign(client, context, session)
    assert signed.status_code == 200, signed.text
    protocol_id = signed.json()["id"]

    rows = ps.signature_rows(db_session, protocol_id)
    doctor_cms = artifacts.get_bytes(cms_artifact(db_session, rows["clinician"]))
    mo_cms = artifacts.get_bytes(cms_artifact(db_session, rows["mo"]))
    ps.replace_signature(
        db_session,
        rows["clinician"],
        cert_der=rows["clinician"].cert_der,
        cms_der=mo_cms,
        protocol_id=protocol_id,
        session_id=session["id"],
    )
    ps.replace_signature(
        db_session,
        rows["mo"],
        cert_der=rows["mo"].cert_der,
        cms_der=doctor_cms,
        protocol_id=protocol_id,
        session_id=session["id"],
    )

    payload = _verify(client, context, session).json()
    assert payload["status"] == "invalid", payload
    assert [item["status"] for item in payload["signatures"]] == ["invalid", "invalid"]


def test_malformed_cms_is_invalid_not_500(client, db_session):
    """Мусор вместо CMS — статус `cms_malformed`, а не 500."""
    context = ps.ctx(client, "sg-garbage")
    session = ps.approved(client, context)
    signed = _sign(client, context, session)
    assert signed.status_code == 200, signed.text
    protocol_id = signed.json()["id"]

    rows = ps.signature_rows(db_session, protocol_id)
    ps.replace_signature(
        db_session,
        rows["mo"],
        cert_der=rows["mo"].cert_der,
        cms_der=b"\x30\x03\x02\x01\x00",
        protocol_id=protocol_id,
        session_id=session["id"],
    )

    resp = _verify(client, context, session)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "invalid", payload
    reasons = {item["role"]: item["reason"] for item in payload["signatures"]}
    assert "cms_malformed" in reasons["mo"]


def test_broken_cms_signature_is_invalid(client, db_session):
    """Испорченное значение подписи в валидном по форме CMS — `signature_invalid`."""
    context = ps.ctx(client, "sg-broken-sig")
    session = ps.approved(client, context)
    signed = _sign(client, context, session)
    assert signed.status_code == 200, signed.text
    protocol_id = signed.json()["id"]

    rows = ps.signature_rows(db_session, protocol_id)
    der = artifacts.get_bytes(cms_artifact(db_session, rows["clinician"]))
    content_info = cms.asn1cms.ContentInfo.load(der)
    signer_info = content_info["content"]["signer_infos"][0]
    value = bytearray(signer_info["signature"].native)
    value[0] ^= 0xFF
    signer_info["signature"] = bytes(value)

    ps.replace_signature(
        db_session,
        rows["clinician"],
        cert_der=rows["clinician"].cert_der,
        cms_der=content_info.dump(),
        protocol_id=protocol_id,
        session_id=session["id"],
    )

    payload = _verify(client, context, session).json()
    assert payload["status"] == "invalid", payload
    reasons = {item["role"]: item["reason"] for item in payload["signatures"]}
    assert "signature_invalid" in reasons["clinician"]


def test_leaf_certificate_parses_with_independent_parser(tmp_path):
    """Лист разбирается `cryptography`, СНИЛС — в SAN otherName в обёртке [0]."""
    from cryptography import x509 as cryptography_x509

    authority = ca.authority(tmp_path / "ca")
    signer = authority.issue(
        "clinician",
        ca.Identity("Петров Пётр", "Врач-психиатр", "МО", "12345678901"),
        365,
    )

    certificate = cryptography_x509.load_der_x509_certificate(signer.der)
    san = certificate.extensions.get_extension_for_class(
        cryptography_x509.SubjectAlternativeName
    ).value
    names = san.get_values_for_type(cryptography_x509.OtherName)
    assert len(names) == 1, san
    assert names[0].type_id.dotted_string == "1.2.643.100.3"
    # Значение в [0] EXPLICIT: внутри UTF8String (тег 0x0c), а не «сырой» текст.
    assert names[0].value[:2] == b"\x0c\x0b", names[0].value
    assert names[0].value[2:] == b"12345678901"
    assert signing_x509.parse(signer.der).snils == "12345678901"


def test_trailing_bytes_in_cms_is_malformed(client, db_session):
    """Мусор после `SignedData` не принимается: контейнер уходит в РЭМД (P5)."""
    context = ps.ctx(client, "sg-trailing")
    session = ps.approved(client, context)
    signed = _sign(client, context, session)
    assert signed.status_code == 200, signed.text
    protocol_id = signed.json()["id"]

    rows = ps.signature_rows(db_session, protocol_id)
    der = artifacts.get_bytes(cms_artifact(db_session, rows["mo"]))
    ps.replace_signature(
        db_session,
        rows["mo"],
        cert_der=rows["mo"].cert_der,
        cms_der=der + b"\x00",
        protocol_id=protocol_id,
        session_id=session["id"],
    )

    payload = _verify(client, context, session).json()
    assert payload["status"] == "invalid", payload
    reasons = {item["role"]: item["reason"] for item in payload["signatures"]}
    assert "cms_malformed" in reasons["mo"]


def test_foreign_content_type_is_rejected(tmp_path):
    """Подписанные атрибуты с `content-type ≠ id-data` не проходят проверку.

    Контейнер пересобран корректной подписью того же ключа, поэтому ловится
    именно проверка типа содержимого (§7.1 обещает id-data), а не подпись.
    """
    from asn1crypto import cms as asn1cms
    from asn1crypto import core

    authority = ca.authority(tmp_path / "ca")
    signer = authority.issue("clinician", ca.Identity("Петров Пётр", "Врач", "МО"), 30)
    content = b"<ClinicalDocument/>"
    der = cms.sign(content, signer)
    assert cms.verify_cms(content, der)[0] is True

    info = asn1cms.ContentInfo.load(der)
    signer_info = info["content"]["signer_infos"][0]
    attributes = signer_info["signed_attrs"]
    for attribute in attributes:
        if attribute["type"].native == "content_type":
            attribute["values"] = [asn1cms.ContentType("1.2.840.113549.1.9.16.1.4")]
    signature = gost.sign(signer.private_key, gost.digest(attributes.untag().dump()))
    signer_info["signature"] = core.OctetString(gost.to_x509_signature(signature))

    ok, _info, reason = cms.verify_cms(content, info.dump())
    assert ok is False
    assert reason == cms.REASON_CONTENT_TYPE, reason
    assert cms.verify_cms(content, der + b"\x00")[2] == cms.REASON_FORMAT

