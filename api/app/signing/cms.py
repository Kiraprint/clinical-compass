"""FR-40 (docs/18 §7): detached CMS SignedData (CAdES-BES-подобный) на asn1crypto.

Формат — то, что РЭМД принимает рядом с СЭМД: отсоединённая подпись (`*.xml.sig`),
`eContent` **отсутствует** (detached), подписываются атрибуты
`content-type` + `message-digest` + `signing-time`, хэш — Стрибог-256
(`1.2.643.7.1.1.2.2`), подпись — ГОСТ 34.10-2012-256 с тем же хэшем
(`1.2.643.7.1.1.3.2`). Значение подписи в контейнере — `s‖r` (RFC 9215 §3).

Подписываемые атрибуты кодируются как `SET OF` (0x31) при вычислении подписи и
как `[0] IMPLICIT` (0xA0) внутри SignerInfo — это и есть классическая ловушка
CMS: подписывать надо `untag()`, иначе verify не сойдётся.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from asn1crypto import algos, cms as asn1cms, core
from asn1crypto import x509 as asn1x509

from . import ca, gost, x509

_DIGEST_ALG = algos.DigestAlgorithm({"algorithm": gost.OID_GOST3411_2012_256})
_SIGN_ALG = algos.SignedDigestAlgorithm({"algorithm": gost.OID_GOST3410_2012_256_SIGN})

# Причины отказа verify — их видит API (docs/18 §4.5).
REASON_DIGEST = "digest_mismatch"
REASON_SIGNATURE = "signature_invalid"
REASON_FORMAT = "cms_malformed"
REASON_CERT_MISSING = "certificate_missing"
REASON_ALGORITHM = "algorithm_unsupported"
REASON_EMBEDDED = "content_embedded"


@dataclass(frozen=True)
class SignerInfo:
    """Разобранные сведения о подписанте — без закрытых ключей и без текста."""

    issuer: str
    serial_hex: str
    thumbprint: str
    digest: bytes
    signed_at: datetime | None
    certificate: x509.Certificate


def sign(content: bytes, signer: ca.Signer) -> bytes:
    """Отсоединённая CMS-подпись под `content` ключом подписанта."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    attributes = asn1cms.CMSAttributes([
        asn1cms.CMSAttribute({
            "type": "content_type",
            "values": [asn1cms.ContentType("data")],
        }),
        asn1cms.CMSAttribute({
            "type": "message_digest",
            "values": [core.OctetString(gost.digest(content))],
        }),
        asn1cms.CMSAttribute({
            "type": "signing_time",
            "values": [asn1cms.Time({"utc_time": now})],
        }),
    ])
    # Подписываются именно SET OF-байты (0x31), а не IMPLICIT-форма из SignerInfo.
    signature = gost.sign(signer.private_key, gost.digest(attributes.dump()))
    signer_info = asn1cms.SignerInfo({
        "version": "v1",
        "sid": asn1cms.SignerIdentifier({
            "issuer_and_serial_number": asn1cms.IssuerAndSerialNumber({
                "issuer": signer.certificate.parsed.issuer,
                "serial_number": signer.certificate.parsed.serial_number,
            }),
        }),
        "digest_algorithm": _DIGEST_ALG,
        "signed_attrs": attributes,
        "signature_algorithm": _SIGN_ALG,
        "signature": core.OctetString(gost.to_x509_signature(signature)),
    })
    signed_data = asn1cms.SignedData({
        "version": "v1",
        "digest_algorithms": [_DIGEST_ALG],
        # detached: content_type задан, content отсутствует
        "encap_content_info": {"content_type": "data"},
        "certificates": [asn1cms.CertificateChoices({
            "certificate": signer.certificate.parsed,
        })],
        "signer_infos": [signer_info],
    })
    return asn1cms.ContentInfo({
        "content_type": "signed_data",
        "content": signed_data,
    }).dump()


def _attribute(attributes, name: str):
    for attribute in attributes:
        if attribute["type"].native == name:
            return attribute["values"][0]
    return None


def verify_cms(
    content: bytes,
    der: bytes,
) -> tuple[bool, SignerInfo | None, str | None]:
    """Проверить отсоединённую подпись над `content`.

    Возвращает `(ok, signer_info, reason)`. Контракт §7: ни битый DER, ни
    испорченный хэш не должны превращаться в исключение — причина отдаётся
    строкой, решение о статусе принимает сервис подписи.
    """
    try:
        info = asn1cms.ContentInfo.load(der)
        if info["content_type"].native != "signed_data":
            return False, None, REASON_FORMAT
        signed_data = info["content"]
        if len(signed_data["signer_infos"]) != 1:
            return False, None, REASON_FORMAT
        signer_info = signed_data["signer_infos"][0]
        if signed_data["encap_content_info"]["content"].native is not None:
            # В контейнере лежит сам документ: это не detached-подпись РЭМД.
            return False, None, REASON_EMBEDDED
        if signer_info["digest_algorithm"]["algorithm"].dotted != gost.OID_GOST3411_2012_256:
            return False, None, REASON_ALGORITHM
        if (
            signer_info["signature_algorithm"]["algorithm"].dotted
            != gost.OID_GOST3410_2012_256_SIGN
        ):
            return False, None, REASON_ALGORITHM

        certificates = signed_data["certificates"]
        parsed = None
        sid = signer_info["sid"]
        if sid.name != "issuer_and_serial_number":
            return False, None, REASON_ALGORITHM
        issuer_name = sid.chosen["issuer"]
        serial = sid.chosen["serial_number"].native
        for choice in certificates or []:
            if choice.name != "certificate":
                continue
            candidate = x509.parse(choice.chosen.dump())
            if candidate.parsed.serial_number == serial and (
                candidate.parsed.issuer == issuer_name
            ):
                parsed = candidate
                break
        if parsed is None:
            return False, None, REASON_CERT_MISSING

        attributes = signer_info["signed_attrs"]
        digests = _attribute(attributes, "message_digest")
        if digests is None:
            return False, None, REASON_FORMAT
        message_digest = bytes(digests.native)
        signing_time = _attribute(attributes, "signing_time")
        signed_at = (
            signing_time.native if signing_time is not None else None
        )

        info_out = SignerInfo(
            issuer=parsed.issuer,
            serial_hex=parsed.serial_hex,
            thumbprint=parsed.thumbprint,
            digest=message_digest,
            signed_at=signed_at,
            certificate=parsed,
        )
        if message_digest != gost.digest(content):
            return False, info_out, REASON_DIGEST

        signature = bytes(signer_info["signature"].native)
        if len(signature) != gost.SIGNATURE_BYTES:
            return False, info_out, REASON_SIGNATURE
        # Подписывались SET OF-байты атрибутов, в контейнере они IMPLICIT [0].
        signed_bytes = attributes.untag().dump()
        if not gost.verify(
            parsed.public_key,
            gost.digest(signed_bytes),
            gost.from_x509_signature(signature),
        ):
            return False, info_out, REASON_SIGNATURE
        return True, info_out, None
    except Exception:  # noqa: BLE001 — битый DER/ASN.1: это статус, а не 500
        return False, None, REASON_FORMAT


def chain_ok(certificate: x509.Certificate, authority_der: bytes) -> bool:
    """Цепочка до демо-УЦ: имя издателя совпадает и подпись листа верна."""
    try:
        issuer = x509.parse(authority_der)
    except ValueError:
        return False
    return x509.verify_issued_by(certificate, issuer)


def issued_by_subject(certificate: x509.Certificate, authority_der: bytes) -> bool:
    """Издатель листа — именно наш УЦ (без проверки подписи)."""
    try:
        issuer = x509.parse(authority_der)
    except ValueError:
        return False
    return certificate.parsed.issuer == issuer.parsed.subject


__all__ = [
    "SignerInfo",
    "chain_ok",
    "issued_by_subject",
    "sign",
    "verify_cms",
]
