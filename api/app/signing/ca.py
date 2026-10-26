"""FR-40 (docs/18 §7): демо-УЦ «Compass Demo CA» — сертификаты подписантов.

Ничего не выпускается «на лету без памяти»: и сам УЦ, и листовые сертификаты
кэшируются на диске под `CC_ARTIFACTS_DIR/_ca/`, чтобы подпись, поставленная до
рестарта API, проверялась и после него (иначе verify всегда был бы `unknown_ca`).

Дерево каталога (файлы — 0600, каталоги — 0700):

    _ca/ca.pem                  сертификат + ключ УЦ одним файлом (CC_SIGNING_CA_*)
    _ca/leaves/<hash>.pem       листовой сертификат + его ключ одним файлом

Пара «сертификат + ключ» — единый объект: файл собирается во временном имени и
появляется под итоговым через `os.link`, поэтому «смешанной пары» и усечённого
PEM не бывает даже при крахе посреди записи.

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


def _write_pair(path: Path, blocks: list[tuple[str, bytes]]) -> bool:
    """Записать (сертификат, ключ) **одним** файлом атомарно. False — файл уже есть.

    Пара — единый объект: `cert` и `key` обязаны появиться вместе, иначе
    достижима «смешанная пара» (сертификат одного УЦ + ключ другого): `sign`
    отвечает 200, а `verify` навсегда отдаёт `unknown_ca`. Поэтому не два файла,
    а один PEM с двумя блоками, который кладётся под итоговым именем через
    `os.link` — атомарную операцию «файл целиком или ничего»; усечённый файл при
    крахе посреди записи тоже невозможен (временное имя не видно читателям).
    """
    body = b"".join(_pem(data, label) for label, data in blocks)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _read_pair(path: Path) -> tuple[bytes, bytes] | None:
    """DER сертификата и закрытый ключ из одного PEM-файла."""
    blob = _read(path)
    if blob is None:
        return None
    certificate = private_key = None
    try:
        blocks = list(pem.unarmor(blob, multiple=True))
    except ValueError as exc:
        raise RuntimeError(f"{path.name}: файл пары повреждён, удалите {path.parent}") from exc
    for label, _headers, der in blocks:
        if label == "CERTIFICATE":
            certificate = der
        elif label == "PRIVATE KEY":
            private_key = der
    if certificate is None or private_key is None:
        raise RuntimeError(f"{path.name}: ожидались блоки CERTIFICATE и PRIVATE KEY")
    return certificate, private_key


def _matches(certificate: bytes, private_key: bytes) -> bool:
    """Открытый ключ сертификата соответствует закрытому ключу пары."""
    try:
        return x509.parse(certificate).public_key == gost.public_key(private_key)
    except Exception:  # noqa: BLE001 — битый сертификат/ключ = не пара
        return False


def _checked_pair(path: Path) -> tuple[bytes, bytes] | None:
    """Пара с проверкой соответствия ключа сертификату (иначе — явная ошибка)."""
    pair = _read_pair(path)
    if pair is not None and not _matches(*pair):
        raise RuntimeError(f"{path.name}: ключ не соответствует сертификату")
    return pair


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
        if not _matches(*external):
            raise RuntimeError("CC_SIGNING_CA_CERT/KEY: ключ не соответствует сертификату")
        return external

    pair_path = root / "ca.pem"
    pair = _checked_pair(pair_path)
    if pair is not None:
        return pair

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
    if _write_pair(pair_path, [("CERTIFICATE", der), ("PRIVATE KEY", private_key)]):
        return der, private_key
    # Проигравший гонку читает созданную конкурентом пару целиком.
    pair = _checked_pair(pair_path)
    if pair is None:
        raise RuntimeError("демо-УЦ не создан и не найден")
    return pair


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
        pair_path = leaves / f"{cached}.pem"

        pair = _checked_pair(pair_path)
        if pair is None:
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
            if not _write_pair(
                pair_path, [("CERTIFICATE", der), ("PRIVATE KEY", private_key)]
            ):
                competing = _checked_pair(pair_path)
                if competing is None:
                    raise RuntimeError("кэш листового сертификата повреждён")
                der, private_key = competing
        else:
            der, private_key = pair

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
