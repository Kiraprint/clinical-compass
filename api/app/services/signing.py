"""FR-40 (docs/18 §4.5, §7): подпись протокола, PDF и проверка подписи.

Оркестрация трёх действий поверх крипто-пакета `app.signing`:

1. **sign** — только подтверждённый протокол (`approved`), две подписи (врач и
   уполномоченное лицо МО, п. 11 приказа 209н) над одними и теми же байтами
   СЭМД, протокол → `signed`. Идемпотентность: повтор → 409
   `PROTOCOL_ALREADY_SIGNED` (новую версию врач создаёт правкой, §3.4).
2. **pdf** — только подписанный протокол, WeasyPrint по HTML-шаблону, результат
   кэшируется артефактом `kind='protocol_pdf'` (повторный GET отдаёт те же байты).
3. **verify** — пересчёт Стрибог-хэша сохранённых байт СЭМД, разбор обеих
   CMS-подписей, цепочка до демо-УЦ, сроки действия и привязка подписанта.
   Битый/подменённый документ — это **статус**, а не 500.

Закрытые ключи живут только в памяти процесса на время подписи (и в кэше демо-
УЦ под `CC_ARTIFACTS_DIR/_ca/`, 0600). В БД, логи и API уходят только серийный
номер, издатель, срок действия и отпечаток сертификата.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import Problem
from app.models import (
    ClinicalSession,
    DocumentArtifact,
    ProtocolSignature,
    SessionProtocol,
    User,
)
from app.signing import ca, cms, gost, x509

from . import artifacts, audit, documents, protocols
from .sessions import PROTOCOL_ACTIVE, require_patient_access, require_session

DOCUMENT_TITLE = "Протокол приёма врача-психиатра"
CDA_MIME = "application/xml"
CMS_MIME = "application/pkcs7-signature"
PDF_MIME = "application/pdf"
MAX_PDF_BYTES = 10 * 1024 * 1024  # потолок PDF (§4.6)

STATUSES = ("valid", "invalid", "unknown_ca", "expired", "digest_mismatch")
# Порядок «важности»: первый сработавший статус становится итоговым.
_SEVERITY = ("digest_mismatch", "unknown_ca", "expired", "invalid")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _require_backend() -> None:
    """MVP умеет только демо-УЦ; иной бэкенд подписи — честная 503."""
    if settings.signer_backend != "mock-ca":
        raise Problem(
            503,
            "SIGNER_UNAVAILABLE",
            f"Бэкенд подписи {settings.signer_backend!r} не поддерживается",
        )


async def _active_protocol(db: AsyncSession, session_id, lock: bool = False) -> SessionProtocol:
    """Активная версия протокола; нет — 404 (как GET §4.4)."""
    query = (
        select(SessionProtocol)
        .where(
            SessionProtocol.session_id == session_id,
            SessionProtocol.status.in_(PROTOCOL_ACTIVE),
        )
        .order_by(SessionProtocol.version.desc())
        .limit(1)
    )
    if lock:
        query = query.with_for_update()
    row = await db.scalar(query)
    if row is None:
        raise Problem(404, "PROTOCOL_NOT_FOUND", "Протокол ещё не заполнен")
    return row


async def _signatures(db: AsyncSession, protocol_id) -> list[ProtocolSignature]:
    rows = await db.scalars(
        select(ProtocolSignature)
        .where(ProtocolSignature.protocol_id == protocol_id)
        .order_by(ProtocolSignature.role)
    )
    return list(rows)


def _signature_payload(row: ProtocolSignature) -> dict:
    return {
        "id": str(row.id),
        "role": row.role,
        "fio": row.fio,
        "position": row.position,
        "organization": row.organization,
        "snils": row.snils,
        "cert_serial": row.cert_serial,
        "cert_issuer": row.cert_issuer,
        "cert_not_before": _iso(row.cert_not_before),
        "cert_not_after": _iso(row.cert_not_after),
        "cert_thumbprint": row.cert_thumbprint,
        "digest_algorithm": row.digest_algorithm,
        "signature_algorithm": row.signature_algorithm,
        "content_sha256": row.content_sha256,
        "signed_at": _iso(row.signed_at),
        "artifact_id": str(row.cms_artifact_id),
    }


async def summary(db: AsyncSession, protocol_id) -> dict | None:
    """Блок `signature` для SessionDetail (§4.2): нет подписей — None."""
    rows = await _signatures(db, protocol_id)
    if not rows:
        return None
    return {
        "status": "signed",
        "signed_at": _iso(min(row.signed_at for row in rows)),
        "signatures": [_signature_payload(row) for row in rows],
    }


async def _protocol_document(
    db: AsyncSession, session: ClinicalSession, protocol: SessionProtocol, clinician: User
) -> tuple[bytes, str]:
    """Байты СЭМД протокола и hex Стрибог-хэша — ровно то, что подписывается."""
    patient = await db.get(User, session.patient_id)
    payload = protocols.decode_sections(protocol)
    cda = documents.render_cda(
        document_id=str(protocol.id),
        version=protocol.version,
        title=DOCUMENT_TITLE,
        patient=documents.Patient(
            id=str(session.patient_id),
            fio=patient.fio if patient else None,
            birth_date=patient.birth_date if patient else None,
        ),
        clinician=documents.Clinician(
            id=str(clinician.id),
            fio=ca.clinician_identity(clinician.fio).fio,
            position=settings.signing_doctor_position,
        ),
        organization=settings.signing_mo_org,
        effective_at=session.ended_at or session.started_at,
        sections=payload,
    )
    return cda, gost.digest(cda).hex()


async def sign(
    db: AsyncSession, clinician_id, session_id, ip: str | None = None
) -> dict:
    """Подписать подтверждённый протокол двумя подписями (врач + МО)."""
    _require_backend()
    session = await require_session(db, clinician_id, session_id)
    await require_patient_access(db, clinician_id, session.patient_id)
    protocol = await _active_protocol(db, session.id, lock=True)

    if protocol.status == "signed" or await _signatures(db, protocol.id):
        raise Problem(409, "PROTOCOL_ALREADY_SIGNED", "Протокол уже подписан")
    if protocol.status != "approved":
        raise Problem(
            409,
            "PROTOCOL_NOT_APPROVED",
            "Подписать можно только подтверждённый протокол (status=approved)",
        )

    clinician = await db.get(User, clinician_id)
    cda, digest_hex = await _protocol_document(db, session, protocol, clinician)

    try:
        signers = [
            ca.issue_clinician(clinician.fio if clinician else None),
            ca.issue_mo(),
        ]
    except Exception as exc:  # noqa: BLE001 — нет УЦ/ключа → 503, не 500
        raise Problem(503, "SIGNER_UNAVAILABLE", "Демо-УЦ недоступен") from exc

    cda_artifact = artifacts.put_bytes(
        "protocol_cda",
        cda,
        session_id=session.id,
        protocol_id=protocol.id,
        mime=CDA_MIME,
    )
    db.add(cda_artifact)
    await db.flush()  # id артефакта нужен строке подписи до commit

    rows: list[ProtocolSignature] = []
    for signer in signers:
        der = cms.sign(cda, signer)
        artifact = artifacts.put_bytes(
            "signature",
            der,
            session_id=session.id,
            protocol_id=protocol.id,
            mime=CMS_MIME,
        )
        db.add(artifact)
        await db.flush()
        row = ProtocolSignature(
            protocol_id=protocol.id,
            session_id=session.id,
            role=signer.role,
            signer_user_id=clinician_id if signer.role == "clinician" else None,
            fio=signer.identity.fio,
            position=signer.identity.position,
            organization=signer.identity.organization,
            snils=signer.identity.snils,
            cert_serial=signer.certificate.serial_hex,
            cert_issuer=signer.certificate.issuer,
            cert_not_before=signer.certificate.not_before,
            cert_not_after=signer.certificate.not_after,
            cert_thumbprint=signer.certificate.thumbprint,
            cert_der=signer.der,
            cms_artifact_id=artifact.id,
            content_sha256=digest_hex,
            signed_at=_now(),
        )
        db.add(row)
        rows.append(row)

    await db.flush()
    protocol.status = "signed"
    # Ответ собираем до commit: после него ORM-атрибуты истекли (MissingGreenlet).
    signature = {
        "status": "signed",
        "signed_at": _iso(min(row.signed_at for row in rows)),
        "signatures": [_signature_payload(row) for row in rows],
    }
    await audit.record(
        db,
        clinician_id,
        "SESSION_PROTOCOL_SIGN",
        "session_protocol",
        f"{session.id}:v{protocol.version}",
        ip=ip,
    )
    await db.commit()

    payload = await protocols.get(db, clinician_id, session_id)
    payload["signature"] = signature
    return payload


async def _signed_protocol(db: AsyncSession, session_id) -> SessionProtocol | None:
    """Последняя **подписанная** версия: признак — наличие строк подписи.

    Правка после подписи делает старую версию `superseded`, но её подпись и
    документ остаются: статус версии сам по себе не годится как признак.

    Документ и подпись принадлежат версии, а не сессии: черновик v2 не должен
    скрывать подписанный v1 — он остаётся проверяемым и печатаемым (§3.4, §7.4).
    """
    return await db.scalar(
        select(SessionProtocol)
        .join(ProtocolSignature, ProtocolSignature.protocol_id == SessionProtocol.id)
        .where(SessionProtocol.session_id == session_id)
        .order_by(SessionProtocol.version.desc())
        .limit(1)
    )

async def _latest_artifact(db: AsyncSession, protocol_id, kind: str) -> DocumentArtifact | None:
    return await db.scalar(
        select(DocumentArtifact)
        .where(DocumentArtifact.protocol_id == protocol_id, DocumentArtifact.kind == kind)
        .order_by(DocumentArtifact.created_at.desc())
        .limit(1)
    )


def _stamp(row: ProtocolSignature) -> documents.Stamp:
    return documents.Stamp(
        role=row.role,
        fio=row.fio,
        position=row.position,
        snils=row.snils,
        issuer=row.cert_issuer,
        serial=row.cert_serial,
        not_before=row.cert_not_before,
        not_after=row.cert_not_after,
        thumbprint=row.cert_thumbprint,
        signed_at=row.signed_at,
    )


async def pdf(
    db: AsyncSession, clinician_id, session_id, ip: str | None = None
) -> tuple[bytes, str]:
    """Печатная форма подписанного протокола (кэш в артефактах)."""
    session = await require_session(db, clinician_id, session_id)
    await require_patient_access(db, clinician_id, session.patient_id)
    protocol = await _signed_protocol(db, session.id)
    if protocol is None:
        raise Problem(409, "PROTOCOL_NOT_SIGNED", "Документ доступен после подписи")
    rows = await _signatures(db, protocol.id)
    if not rows:
        raise Problem(409, "PROTOCOL_NOT_SIGNED", "Подписи протокола не найдены")

    cached = await _latest_artifact(db, protocol.id, "protocol_pdf")
    if cached is not None:
        # Кэш отдаёт те же байты, но обращение к подписанному документу — тоже
        # событие доступа: аудит пишется на каждый GET, а не только на рендер.
        await audit.record(
            db,
            clinician_id,
            "SESSION_PROTOCOL_PDF",
            "session_protocol",
            f"{session.id}:v{protocol.version}",
            ip=ip,
        )
        await db.commit()
        return artifacts.get_bytes(cached), f"protocol-{protocol.version}.pdf"

    patient = await db.get(User, session.patient_id)
    clinician = await db.get(User, session.clinician_id)
    content = documents.render_pdf(
        document_id=str(protocol.id),
        version=protocol.version,
        title=DOCUMENT_TITLE,
        organization=settings.signing_mo_org,
        effective_at=session.ended_at or session.started_at,
        patient=documents.Patient(
            id=str(session.patient_id),
            fio=patient.fio if patient else None,
            birth_date=patient.birth_date if patient else None,
        ),
        clinician=documents.Clinician(
            id=str(session.clinician_id),
            fio=ca.clinician_identity(clinician.fio if clinician else None).fio,
            position=settings.signing_doctor_position,
        ),
        sections=protocols.decode_sections(protocol),
        stamps=[_stamp(row) for row in rows],
    )
    if len(content) > MAX_PDF_BYTES:
        raise Problem(413, "DOCUMENT_TOO_LARGE", "PDF протокола превысил потолок §4.6")

    artifact = artifacts.put_bytes(
        "protocol_pdf",
        content,
        session_id=session.id,
        protocol_id=protocol.id,
        mime=PDF_MIME,
    )
    db.add(artifact)
    await audit.record(
        db,
        clinician_id,
        "SESSION_PROTOCOL_PDF",
        "session_protocol",
        f"{session.id}:v{protocol.version}",
        ip=ip,
    )
    await db.commit()
    return content, f"protocol-{protocol.version}.pdf"


async def _expected_identity(db: AsyncSession, session: ClinicalSession, role: str):
    """Кто обязан быть подписантом по контракту §7."""
    if role == "clinician":
        clinician = await db.get(User, session.clinician_id)
        return ca.clinician_identity(clinician.fio if clinician else None)
    return ca.mo_identity()


async def _verify_one(
    db: AsyncSession,
    row: ProtocolSignature,
    cda: bytes,
    digest_hex: str,
    session: ClinicalSession,
) -> dict[str, Any]:
    """Статус одной подписи: хэш, цепочка, срок, привязка, сама подпись."""
    base: dict[str, Any] = {
        "id": str(row.id),
        "role": row.role,
        "fio": row.fio,
        "position": row.position,
        "snils": row.snils,
        "cert_serial": row.cert_serial,
        "cert_issuer": row.cert_issuer,
        "cert_thumbprint": row.cert_thumbprint,
        "cert_not_after": _iso(row.cert_not_after),
        "signed_at": _iso(row.signed_at),
        "status": "valid",
        "reason": None,
    }

    if row.content_sha256 != digest_hex:
        return {
            **base,
            "status": "digest_mismatch",
            "reason": "хэш подписанных байт не совпадает",
        }

    try:
        artifact = await db.get(DocumentArtifact, row.cms_artifact_id)
        if artifact is None:
            return {**base, "status": "invalid", "reason": "контейнер подписи не найден"}
        der = artifacts.get_bytes(artifact)
    except Problem as exc:
        return {**base, "status": "invalid", "reason": exc.detail}
    except Exception:  # noqa: BLE001 — пропавший файл → статус, не 500
        return {**base, "status": "invalid", "reason": "контейнер подписи недоступен"}

    try:
        certificate = x509.parse(row.cert_der)
        authority = ca.authority()
    except Exception:  # noqa: BLE001 — битый сертификат/недоступный УЦ → статус
        return {**base, "status": "invalid", "reason": "сертификат подписи не разбирается"}

    authority = ca.authority()
    if not cms.chain_ok(certificate, authority.der):
        return {**base, "status": "unknown_ca", "reason": "сертификат выдан неизвестным УЦ"}
    if certificate.expired():
        return {**base, "status": "expired", "reason": "срок действия сертификата истёк"}

    ok, _info, reason = cms.verify_cms(cda, der)
    if reason == cms.REASON_DIGEST:
        return {**base, "status": "digest_mismatch", "reason": "хэш документа не совпадает"}
    if not ok:
        return {**base, "status": "invalid", "reason": f"подпись недействительна ({reason})"}

    expected = await _expected_identity(db, session, row.role)
    bound = row.fio == expected.fio and row.position == expected.position
    if row.role == "clinician":
        bound = bound and row.signer_user_id == session.clinician_id
    if not bound:
        return {
            **base,
            "status": "invalid",
            "reason": "подпись не принадлежит ожидаемому подписанту",
        }
    return base


async def verify(db: AsyncSession, clinician_id, session_id) -> dict:
    """Проверить обе подписи протокола; любая аномалия — статус, не исключение."""
    session = await require_session(db, clinician_id, session_id)
    await require_patient_access(db, clinician_id, session.patient_id)
    protocol = await _signed_protocol(db, session.id)

    payload: dict[str, Any] = {
        "status": "valid",
        "reason": None,
        "document": {
            "id": str(protocol.id) if protocol else None,
            "version": protocol.version if protocol else None,
            "digest": None,
            # Не `sha256`: здесь Стрибог-256, проверять надо ГОСТ-хэшем.
            "digest_algorithm": "GOST3411-2012-256",
        },
        "signatures": [],
    }
    if protocol is None:
        return {**payload, "status": "invalid", "reason": "подписанный протокол не найден"}

    cda_artifact = await _latest_artifact(db, protocol.id, "protocol_cda")
    if cda_artifact is None:
        return {**payload, "status": "invalid", "reason": "документ протокола не найден"}
    try:
        cda = artifacts.get_bytes(cda_artifact)
    except Problem as exc:
        return {**payload, "status": "invalid", "reason": exc.detail}
    digest_hex = gost.digest(cda).hex()
    payload["document"]["digest"] = digest_hex

    rows = {row.role: row for row in await _signatures(db, protocol.id)}
    missing = [role for role in ("clinician", "mo") if role not in rows]
    if missing:
        return {**payload, "status": "invalid", "reason": f"нет подписи: {', '.join(missing)}"}

    results = [await _verify_one(db, rows[role], cda, digest_hex, session) for role in ("clinician", "mo")]
    payload["signatures"] = results

    for status in _SEVERITY:
        failed = next((item for item in results if item["status"] == status), None)
        if failed is not None:
            return {**payload, "status": status, "reason": f"{failed['role']}: {failed['reason']}"}
    return payload
