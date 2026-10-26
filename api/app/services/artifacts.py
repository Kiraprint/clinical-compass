"""FR-38…41 (docs/18 §4.6): шифрованные артефакты сессий — файлы на диске.

Аудио, расшифровки, PDF и подпись — спецкатегория ПДн (152-ФЗ, ПП №1119): на
диске только AES-256-GCM, ключ — HKDF-SHA256 от того же `CC_DATA_KEY`, что и
pgcrypto (единый секрет инсталляции, отдельная ротация не нужна).

Формат файла: `CCART1` (6 байт) + 12-байт nonce + ciphertext (включает тег GCM).
`sha256` в БД считается по шифротексту — сверка целостности перед расшифровкой.
Имя файла — `{kind}/{uuid}.bin`, без ПДн. Чтение только через `get_bytes`:
внешний код не открывает файлы каталога напрямую.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import settings
from app.crypto import data_key
from app.errors import Problem
from app.models import DocumentArtifact

MAGIC = b"CCART1"  # версия формата: заголовок читается до nonce
NONCE_BYTES = 12  # AES-GCM: 96-бит nonce (RFC 5116 §5.1)
KEY_BYTES = 32  # AES-256
HKDF_INFO = b"cc-artifacts-v1"  # смена info = смена поколения ключа артефактов
TAG_BYTES = 16

# §3.6: закрытый список видов — ограничение задано в БД (CHECK) и здесь, чтобы
# ошибка была 422, а не 500 от constraint.
KINDS = ("audio", "transcript", "protocol_pdf", "protocol_cda", "signature")


def _key() -> bytes:
    """Ключ артефактов: HKDF-SHA256(CC_DATA_KEY, info) — не равен ключу pgcrypto."""
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=HKDF_INFO
    ).derive(data_key().encode())


def _root() -> Path:
    return Path(settings.artifacts_dir)


def _seal(data: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return MAGIC + nonce + AESGCM(_key()).encrypt(nonce, data, None)


def _open(blob: bytes) -> bytes:
    if not blob.startswith(MAGIC):
        raise Problem(500, "ARTIFACT_CORRUPT", "Неизвестный формат артефакта")
    nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_BYTES]
    if len(nonce) != NONCE_BYTES:
        raise Problem(500, "ARTIFACT_CORRUPT", "Артефакт обрезан (нет nonce)")
    try:
        return AESGCM(_key()).decrypt(nonce, blob[len(MAGIC) + NONCE_BYTES :], None)
    except InvalidTag:
        # Подмена байта = провал аутентификации GCM: молча возвращать мусор нельзя.
        raise Problem(500, "ARTIFACT_CORRUPT", "Артефакт повреждён или подменён")


def put_bytes(
    kind: str,
    data: bytes,
    *,
    session_id: uuid.UUID | None = None,
    protocol_id: uuid.UUID | None = None,
    mime: str,
) -> DocumentArtifact:
    """Зашифровать `data` в `CC_ARTIFACTS_DIR/{kind}/{uuid}.bin` и вернуть метаданные.

    Строка в БД не добавляется — вызывающий сервис сам делает `db.add(...)` в
    своей транзакции (файл к этому моменту уже на диске; commit вызывающего
    может откатиться — тогда файл остаётся сиротой без ссылок, что безопаснее
    ссылки на отсутствующий файл).
    """
    if kind not in KINDS:
        raise Problem(422, "ARTIFACT_KIND_UNKNOWN", f"Неизвестный вид артефакта: {kind}")
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise Problem(422, "ARTIFACT_DATA_INVALID", "Ожидались байты")

    blob = _seal(bytes(data))
    root = _root()
    rel = f"{kind}/{uuid.uuid4().hex}.bin"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    # Атомарная запись: недописанный файл не должен попасть в метаданные.
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    return DocumentArtifact(
        kind=kind,
        session_id=session_id,
        protocol_id=protocol_id,
        path=rel,
        sha256=hashlib.sha256(blob).hexdigest(),  # хэш шифротекста (§3.6)
        size_bytes=len(blob),
        mime=mime,
        encrypted=True,
    )


def get_bytes(artifact: DocumentArtifact) -> bytes:
    """Прочитать и расшифровать артефакт; подмена/порча → 500 ARTIFACT_CORRUPT."""
    root = _root().resolve()
    path = (root / artifact.path).resolve()
    # path из БД — относительный (§3.6); побег из каталога = порча данных/атака.
    if root not in path.parents:
        raise Problem(500, "ARTIFACT_CORRUPT", "Путь артефакта вне CC_ARTIFACTS_DIR")
    try:
        blob = path.read_bytes()
    except FileNotFoundError:
        raise Problem(500, "ARTIFACT_MISSING", "Файл артефакта отсутствует")
    if hashlib.sha256(blob).hexdigest() != artifact.sha256:
        raise Problem(500, "ARTIFACT_CORRUPT", "Хэш артефакта не совпадает с метаданными")
    return _open(blob)


def remove(artifact: DocumentArtifact) -> None:
    """Удалить файл артефакта (отзыв согласия, docs/18 §4.6). Идемпотентно."""
    root = _root().resolve()
    path = (root / artifact.path).resolve()
    if root not in path.parents:
        return
    path.unlink(missing_ok=True)
