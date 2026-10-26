"""FR-40 (docs/18 §7): демо-УЦ «Compass Demo CA» — сертификаты подписантов.

Ничего не выпускается «на лету без памяти»: и сам УЦ, и листовые сертификаты
кэшируются на диске под `CC_ARTIFACTS_DIR/_ca/`, чтобы подпись, поставленная до
рестарта API, проверялась и после него (иначе verify всегда был бы `unknown_ca`).

Дерево каталога (файлы — 0600, каталоги — 0700):

    _ca/ca.crt.pem              сертификат УЦ (подменяется через CC_SIGNING_CA_CERT)
    _ca/ca.key.pem              закрытый ключ УЦ (CC_SIGNING_CA_KEY)
    _ca/leaves/<hash>.crt.pem   листовой сертификат подписанта
    _ca/leaves/<hash>.key.pem   его закрытый ключ

Ключи никогда не попадают в git, в БД, в логи и в API-ответы (docs/18 §1, §7):
наружу отдаются только серийный номер, издатель, срок действия и отпечаток.

Имя файла листа — sha256 от личности подписанта и срока действия сертификата:
смена `CC_SIGNING_CERT_DAYS` выпускает новый сертификат, повторный вызов с той
же личностью берёт существующий.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from asn1crypto import pem

from . import gost, x509

CA_YEARS = 10  # УЦ обязан быть валиден заведомо дольше листа
CA_COMMON_NAME = "Compass Demo CA"
CA_ORGANIZATION = "Клинический компас (демонстрационный УЦ)"
CLOCK_SKEW = timedelta(days=1)  # подпись может ставиться при отстающих часах


@dataclass(frozen=True)
class Identity:
    """Кто подписывает: ФИО, должность, организация, СНИЛС (может не быть)."""

    fio: str
    position: str
    organization: str
    snils: str | None = None

    def cache_key(self, days: int, ca_thumbprint: str) -> str:
        """Ключ кэша листа: личность + срок + **отпечаток УЦ**.

        Отпечаток обязателен: после смены УЦ (или удаления `_ca/`) старый лист
        не должен переиспользоваться — иначе `sign` проходит, а `verify` навсегда
        отдаёт `unknown_ca`.
        """
        raw = "\x1f".join(
            (
                ca_thumbprint,
                self.fio,
                self.position,
                self.organization,
                self.snils or "",
                str(days),
            )
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Signer:
    """Выпущенный листовой сертификат вместе с закрытым ключом (только память)."""

    role: str  # "clinician" | "mo"
    identity: Identity
    certificate: x509.Certificate
    private_key: bytes  # закрытый ключ ГОСТ; наружу не отдаётся никогда
    der: bytes


def _root() -> Path:
    return Path(settings.artifacts_dir) / "_ca"


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _pem(data: bytes, label: str) -> bytes:
    return pem.armor(label, data)


def _unpem(blob: bytes) -> bytes:
    _label, _headers, der = pem.unarmor(blob)
    return der


def _create_exclusive(path: Path, data: bytes) -> bool:
    """Создать файл 0600 ровно один раз. False — файл уже существует.

    O_EXCL на **итоговом** имени (а не на временном): два процесса, стартующих
    одновременно (`WEB_CONCURRENCY>1`), не могут перезаписать друг другу УЦ или
    листовой ключ — проигравший гонку просто читает готовый файл.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return True


def _load_external_ca() -> tuple[bytes, bytes] | None:
    """CC_SIGNING_CA_CERT + CC_SIGNING_CA_KEY: чужой УЦ (PEM-файлы на диске)."""
    if not (settings.signing_ca_cert and settings.signing_ca_key):
        return None
    return (
        _unpem(Path(settings.signing_ca_cert).read_bytes()),
        _unpem(Path(settings.signing_ca_key).read_bytes()),
    )


def _load_or_create_ca(root: Path) -> tuple[bytes, bytes]:
    """DER сертификата и закрытый ключ УЦ: env → кэш → новый самоподписанный."""
    external = _load_external_ca()
    if external is not None:
        return external

    cert_path, key_path = root / "ca.crt.pem", root / "ca.key.pem"
    cert_blob, key_blob = _read(cert_path), _read(key_path)
    if cert_blob is not None and key_blob is not None:
        return _unpem(cert_blob), _unpem(key_blob)

    private_key = gost.new_private_key()
    now = datetime.now(timezone.utc)
    der = x509.build(
        subject=x509.subject_name(CA_COMMON_NAME, CA_ORGANIZATION, position=None),
        issuer=None,  # самоподписанный
        serial=secrets.randbits(63) or 1,
        public_key=gost.public_key(private_key),
        private_key=private_key,
        not_before=now - CLOCK_SKEW,
        not_after=now + timedelta(days=365 * CA_YEARS),
        ca=True,
    )
    _mkdir(root)
    # Проигравший гонку процесс перечитывает созданную пару, а не перезаписывает.
    created = _create_exclusive(cert_path, _pem(der, "CERTIFICATE"))
    _create_exclusive(key_path, _pem(private_key, "PRIVATE KEY"))
    if not created:
        cert_blob, key_blob = _read(cert_path), _read(key_path)
        if cert_blob is None or key_blob is None:
            raise RuntimeError("демо-УЦ повреждён: нет пары ca.crt.pem/ca.key.pem")
        return _unpem(cert_blob), _unpem(key_blob)
    return der, private_key


@dataclass(frozen=True)
class Authority:
    """Загруженный демо-УЦ: сертификат и ключ (в памяти, не в логах)."""

    root: Path
    der: bytes
    certificate: x509.Certificate
    private_key: bytes

    def issue(self, role: str, identity: Identity, days: int) -> Signer:
        """Выпустить (или взять из кэша) листовой сертификат подписанта."""
        leaves = _mkdir(self.root / "leaves")
        cached = identity.cache_key(days, self.certificate.thumbprint)
        cert_path, key_path = leaves / f"{cached}.crt.pem", leaves / f"{cached}.key.pem"

        cert_blob, key_blob = _read(cert_path), _read(key_path)
        if cert_blob is not None and key_blob is not None:
            der = _unpem(cert_blob)
            return Signer(
                role=role,
                identity=identity,
                certificate=x509.parse(der),
                private_key=_unpem(key_blob),
                der=der,
            )

        private_key = gost.new_private_key()
        now = datetime.now(timezone.utc)
        der = x509.build(
            subject=x509.subject_name(
                identity.fio,
                identity.organization,
                position=identity.position,
            ),
            issuer=self.certificate.parsed.subject,
            serial=secrets.randbits(63) or 1,
            public_key=gost.public_key(private_key),
            private_key=self.private_key,  # лист подписывает УЦ
            # Отрицательный CC_SIGNING_CERT_DAYS выпускает просроченный лист:
            # окно действия сдвигается в прошлое, а не становится нулевым.
            not_after=now + timedelta(days=days),
            not_before=min(now - CLOCK_SKEW, now + timedelta(days=days) - CLOCK_SKEW),
            ca=False,
            snils=identity.snils,
        )
        # Проигравший гонку берёт готовую пару: сертификат и ключ всегда от одного УЦ.
        created = _create_exclusive(cert_path, _pem(der, "CERTIFICATE"))
        _create_exclusive(key_path, _pem(private_key, "PRIVATE KEY"))
        if not created:
            cert_blob, key_blob = _read(cert_path), _read(key_path)
            if cert_blob is None or key_blob is None:
                raise RuntimeError("кэш листового сертификата повреждён")
            der = _unpem(cert_blob)
            private_key = _unpem(key_blob)
        return Signer(
            role=role,
            identity=identity,
            certificate=x509.parse(der),
            private_key=private_key,
            der=der,
        )


def authority(root: Path | None = None) -> Authority:
    """Демо-УЦ инсталляции. `root` — подмена каталога (тесты «чужой УЦ»)."""
    base = _mkdir(root) if root is not None else _root()
    der, private_key = _load_or_create_ca(base)
    return Authority(
        root=base, der=der, certificate=x509.parse(der), private_key=private_key
    )


def certificate_days() -> int:
    """Срок жизни листа: CC_SIGNING_CERT_DAYS (отрицательный = уже просрочен)."""
    return int(settings.signing_cert_days)


def mo_identity() -> Identity:
    """Уполномоченное лицо МО — вторая подпись (п. 11 приказа Минздрава 209н)."""
    return Identity(
        fio=settings.signing_mo_fio,
        position=settings.signing_mo_position,
        organization=settings.signing_mo_org,
        snils=settings.signing_mo_snils or None,
    )


def clinician_identity(fio: str | None) -> Identity:
    """Врач-подписант: ФИО из карточки пользователя (пустое — не выдумываем)."""
    return Identity(
        fio=(fio or "").strip() or "Врач без указанного ФИО",
        position=settings.signing_doctor_position,
        organization=settings.signing_mo_org,
        snils=settings.signing_doctor_snils or None,
    )


def issue_clinician(fio: str | None, root: Path | None = None) -> Signer:
    return authority(root).issue("clinician", clinician_identity(fio), certificate_days())


def issue_mo(root: Path | None = None) -> Signer:
    return authority(root).issue("mo", mo_identity(), certificate_days())
