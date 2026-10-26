"""FR-40 (docs/18 §7): X.509 с ГОСТ-ключами по RFC 9215 (asn1crypto).

Стандартный X.509 с ключами ГОСТ Р 34.10-2012 выпускается **целиком** —
двухслойный обходной путь (RSA-сертификат + привязка ГОСТ-ключа в CMS) не
понадобился, поэтому в §7 зафиксирован именно этот вариант:

- SubjectPublicKeyInfo: `algorithm = 1.2.643.7.1.1.1.1`
  (gostr3410-2012-256), параметры — SEQUENCE с OID кривой paramSetB
  (`1.2.643.7.1.2.1.1.2`), ключ — BIT STRING с DER `OCTET STRING` из 64 байт
  `X‖Y` little-endian (RFC 9215 §4.3);
- signatureAlgorithm: `1.2.643.7.1.1.3.2` (ГОСТ 34.10-2012-256 + Стрибог-256),
  параметры отсутствуют; signatureValue — 64 байта `s‖r` (RFC 9215 §3);
- СНИЛС — `subjectAltName` `otherName` с OID `1.2.643.100.3` (как в
  квалифицированных сертификатах аккредитованных УЦ); если СНИЛС подписанта
  неизвестен, расширение не добавляется — значение не выдумывается.

Чего здесь **нет** (и не может быть без аккредитованного УЦ): квалифицированных
расширений (RFC 9215 §5), политик УЦ, CRL/OCSP, цепочки доверия ЕГИСЗ.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from asn1crypto import algos, core, keys
from asn1crypto import x509 as asn1x509

from . import gost

OID_SNILS = "1.2.643.100.3"  # СНИЛС в otherName (OID УЦ, не изобретённый)

_SIGN_ALG = algos.SignedDigestAlgorithm({"algorithm": gost.OID_GOST3410_2012_256_SIGN})


class GostKeyParams(core.Sequence):
    """SubjectPublicKeyInfo.parameters для ГОСТ 34.10-2012 (RFC 9215 §4.2)."""

    _fields = [
        ("public_key_param_set", core.ObjectIdentifier),
        ("digest_param_set", core.ObjectIdentifier, {"optional": True}),
    ]


# asn1crypto не знает ГОСТ-алгоритмы: без этой правки разбор сертификата падает
# KeyError на `subject_public_key_info['public_key']` (ключ — BIT STRING,
# содержащий DER OCTET STRING, а не готовое ключевое значение).
_default_public_key_spec = keys.PublicKeyInfo._spec_callbacks["public_key"]


def _public_key_spec(self: keys.PublicKeyInfo):
    if self["algorithm"]["algorithm"].dotted == gost.OID_GOST3410_2012_256:
        return core.OctetBitString
    return _default_public_key_spec(self)


keys.PublicKeyInfo._spec_callbacks["public_key"] = _public_key_spec


@dataclass(frozen=True)
class Certificate:
    """Разобранный сертификат: наружу отдаём только общедоступные поля."""

    der: bytes
    parsed: asn1x509.Certificate
    serial_hex: str
    issuer: str
    subject: str
    fio: str | None
    position: str | None
    organization: str | None
    snils: str | None
    not_before: datetime
    not_after: datetime
    is_ca: bool
    public_key: bytes  # X‖Y big-endian — в формате gostcrypto

    @property
    def thumbprint(self) -> str:
        """SHA-256 отпечаток DER (hex) — публичный идентификатор без ПДн."""
        return hashlib.sha256(self.der).hexdigest()

    def expired(self, at: datetime | None = None) -> bool:
        moment = at or datetime.now(timezone.utc)
        return moment < self.not_before or moment > self.not_after


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def subject_name(
    fio: str,
    organization: str,
    position: str | None,
) -> asn1x509.Name:
    """Subject: CN=ФИО, O=МО, T=должность (СНИЛС — только в SAN, без дубля)."""
    values: dict[str, str] = {
        "common_name": fio,
        "organization_name": organization,
        "country_name": "RU",
    }
    if position:
        values["title"] = position
    return asn1x509.Name.build(values)


def _extensions(ca: bool, spki: keys.PublicKeyInfo, snils: str | None) -> asn1x509.Extensions:
    items = [
        asn1x509.Extension({
            "extn_id": "basic_constraints",
            "critical": True,
            "extn_value": asn1x509.BasicConstraints({"ca": ca}),
        }),
        asn1x509.Extension({
            "extn_id": "key_usage",
            "critical": True,
            "extn_value": (
                {"key_cert_sign", "crl_sign"}
                if ca
                else {"digital_signature", "non_repudiation"}
            ),
        }),
        asn1x509.Extension({
            "extn_id": "key_identifier",
            "critical": False,
            # SubjectKeyIdentifier — OCTET STRING (SHA-1 открытого ключа, RFC 5280).
            "extn_value": core.OctetString(hashlib.sha1(spki.dump()).digest()),
        }),
    ]
    if snils:
        items.append(
            asn1x509.Extension({
                "extn_id": "subject_alt_name",
                "critical": False,
                "extn_value": asn1x509.GeneralNames([
                    asn1x509.GeneralName({
                        "other_name": asn1x509.AnotherName({
                            "type_id": OID_SNILS,
                            "value": core.Any(core.UTF8String(snils)),
                        }),
                    }),
                ]),
            })
        )
    return asn1x509.Extensions(items)


def build(
    *,
    subject: asn1x509.Name,
    issuer: asn1x509.Name | None,
    serial: int,
    public_key: bytes,
    private_key: bytes,
    not_before: datetime,
    not_after: datetime,
    ca: bool,
    snils: str | None = None,
) -> bytes:
    """Собрать и подписать сертификат; `issuer=None` — самоподписанный УЦ.

    `private_key` — ключ подписанта (УЦ для листа, собственный для УЦ).
    """
    spki = keys.PublicKeyInfo({
        "algorithm": keys.PublicKeyAlgorithm({
            "algorithm": gost.OID_GOST3410_2012_256,
            "parameters": GostKeyParams({
                "public_key_param_set": gost.OID_CURVE_PARAMSET_B_256,
            }),
        }),
        # RFC 9215 §4.3: BIT STRING содержит DER OCTET STRING(64).
        "public_key": core.OctetBitString(
            b"\x04\x40" + gost.to_x509_public_key(public_key)
        ),
    })

    tbs = asn1x509.TbsCertificate({
        "version": "v3",
        "serial_number": serial,
        "signature": _SIGN_ALG,
        "issuer": subject if issuer is None else issuer,
        "validity": {
            "not_before": asn1x509.Time({"utc_time": _aware(not_before)}),
            "not_after": asn1x509.Time({"utc_time": _aware(not_after)}),
        },
        "subject": subject,
        "subject_public_key_info": spki,
        "extensions": _extensions(ca, spki, snils),
    })
    signature = gost.sign(private_key, gost.digest(tbs.dump()))
    certificate = asn1x509.Certificate({
        "tbs_certificate": tbs,
        "signature_algorithm": _SIGN_ALG,
        "signature_value": core.OctetBitString(gost.to_x509_signature(signature)),
    })
    return certificate.dump()


def _name_text(name: asn1x509.Name, key: str) -> str | None:
    value = name.native.get(key)
    return str(value) if value else None


def _snils(parsed: asn1x509.Certificate) -> str | None:
    try:
        names = parsed.subject_alt_name_value
    except (ValueError, KeyError):
        return None
    for name in names or []:
        if name.name != "other_name":
            continue
        other = name.chosen
        if other["type_id"].dotted != OID_SNILS:
            continue
        try:
            return str(other["value"].parsed.native)
        except Exception:  # noqa: BLE001 — значение может быть не UTF8String
            return None
    return None


def parse(der: bytes) -> Certificate:
    """Разобрать DER; неподдерживаемый сертификат → ValueError (а не 500)."""
    if not der:
        raise ValueError("пустой сертификат")
    parsed = asn1x509.Certificate.load(der)
    spki = parsed["tbs_certificate"]["subject_public_key_info"]
    algorithm = spki["algorithm"]["algorithm"].dotted
    if algorithm != gost.OID_GOST3410_2012_256:
        raise ValueError(f"неподдерживаемый алгоритм ключа: {algorithm}")

    raw = bytes(spki["public_key"].native)
    if len(raw) != 66 or raw[:2] != b"\x04\x40":
        raise ValueError("ключ сертификата не OCTET STRING(64)")
    public = gost.from_x509_public_key(raw[2:])

    subject = parsed.subject
    basic = parsed.basic_constraints_value
    validity = parsed["tbs_certificate"]["validity"]
    return Certificate(
        der=der,
        parsed=parsed,
        serial_hex=f"{parsed.serial_number:X}",
        issuer=parsed.issuer.human_friendly,
        subject=subject.human_friendly,
        fio=_name_text(subject, "common_name"),
        position=_name_text(subject, "title"),
        organization=_name_text(subject, "organization_name"),
        snils=_snils(parsed),
        not_before=_aware(validity["not_before"].native),
        not_after=_aware(validity["not_after"].native),
        is_ca=bool(basic and basic["ca"].native),
        public_key=public,
    )


def verify_issued_by(leaf: Certificate, issuer: Certificate) -> bool:
    """Подпись `leaf` ключом `issuer` + совпадение имени издателя."""
    tbs = leaf.parsed["tbs_certificate"]
    if tbs["issuer"] != issuer.parsed.subject:
        return False
    signature = bytes(leaf.parsed["signature_value"].native)
    if len(signature) != gost.SIGNATURE_BYTES:
        return False
    return gost.verify(
        issuer.public_key, gost.digest(tbs.dump()), gost.from_x509_signature(signature)
    )


def verify_self_signed(cert: Certificate) -> bool:
    return verify_issued_by(cert, cert)
