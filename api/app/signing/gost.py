"""FR-40 (docs/18 §7): ГОСТ Р 34.10-2012 / 34.11-2012 — тонкая обёртка над gostcrypto.

Единственное место, где приложение касается примитивов подписи. Всё, что выше
(ca/x509/cms), работает с байтами фиксированной длины и не знает про curve
objects.

Кодировки двух миров различаются, и это единственная причина, почему обёртка
существует:

- **gostcrypto** отдаёт открытый ключ `X‖Y` и подпись `r‖s`, обе части —
  big-endian (`int_to_bytearray`);
- **X.509/CMS** (RFC 9215 §4.3 и §3) требуют открытый ключ `X‖Y`
  **little-endian** и подпись `s‖r` big-endian.

Рукописная правка байтов в сервисе — источник невоспроизводимых «почти верных»
подписей, поэтому конвертации живут здесь, рядом с примитивами.
"""

from __future__ import annotations

import secrets

from gostcrypto import gosthash, gostsignature
from gostcrypto.gostsignature import CURVES_R_1323565_1_024_2019

# OID (RFC 9215 §2): алгоритмы и параметры кривой.
OID_GOST3410_2012_256 = "1.2.643.7.1.1.1.1"  # ключ подписи
OID_GOST3411_2012_256 = "1.2.643.7.1.1.2.2"  # Стрибог-256
OID_GOST3410_2012_256_SIGN = "1.2.643.7.1.1.3.2"  # подпись с Стрибог-256
OID_CURVE_PARAMSET_B_256 = "1.2.643.7.1.2.1.1.2"  # id-tc26-gost-3410-2012-256-paramSetB

KEY_BYTES = 32  # закрытый ключ
SIGNATURE_BYTES = 64  # r‖s по 32 байта

_CURVE = CURVES_R_1323565_1_024_2019["id-tc26-gost-3410-2012-256-paramSetB"]
_SIGNER = gostsignature.new(gostsignature.MODE_256, _CURVE)


def new_private_key() -> bytes:
    """Закрытый ключ: равномерное число из [1, q-1], 32 байта big-endian.

    gostcrypto не умеет генерировать ключ (только публичный из приватного), а
    ГПСЧ R 1323565.1.006-2017 требует внешнего seed — для демо-УЦ берём
    `secrets` (CSPRNG ОС): распределение то же, что у штатного генератора.
    """
    q = _CURVE["q"]
    return (secrets.randbelow(q - 2) + 1).to_bytes(KEY_BYTES, "big")


def public_key(private_key: bytes) -> bytes:
    """Открытый ключ `X‖Y` (big-endian, формат gostcrypto)."""
    return bytes(_SIGNER.public_key_generate(private_key))


def digest(data: bytes) -> bytes:
    """ГОСТ Р 34.11-2012 (Стрибог-256), 32 байта."""
    return bytes(gosthash.new("streebog256", data=data).digest())


def sign(private_key: bytes, digest_value: bytes) -> bytes:
    """Подпись хэша: 64 байта `r‖s` (порядок gostcrypto)."""
    return bytes(_SIGNER.sign(private_key, digest_value))


def verify(public: bytes, digest_value: bytes, signature: bytes) -> bool:
    """Проверка подписи в формате gostcrypto (`r‖s`). Битые входы → False."""
    try:
        return bool(_SIGNER.verify(public, digest_value, signature))
    except Exception:  # noqa: BLE001 — gostcrypto бросает на длине/диапазоне
        return False


def to_x509_public_key(public: bytes) -> bytes:
    """X‖Y big-endian → X‖Y little-endian (RFC 9215 §4.3)."""
    return bytes(reversed(public[:32])) + bytes(reversed(public[32:]))


def from_x509_public_key(public: bytes) -> bytes:
    """X‖Y little-endian из сертификата → X‖Y big-endian для gostcrypto."""
    return bytes(reversed(public[:32])) + bytes(reversed(public[32:]))


def to_x509_signature(signature: bytes) -> bytes:
    """`r‖s` → `s‖r` (RFC 9215 §3: порядок значения подписи в X.509/CMS)."""
    return signature[32:] + signature[:32]


def from_x509_signature(signature: bytes) -> bytes:
    """`s‖r` из X.509/CMS → `r‖s` для gostcrypto (обратное к to_x509_signature)."""
    return signature[32:] + signature[:32]
