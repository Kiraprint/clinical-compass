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
import shutil
import time
import uuid
from pathlib import Path
from typing import Iterable, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
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
CHUNK_BYTES = 1024 * 1024  # размер порции при потоковой записи/чтении

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


def put_stream(
    kind: str,
    chunks: Iterable[bytes],
    *,
    session_id: uuid.UUID | None = None,
    protocol_id: uuid.UUID | None = None,
    mime: str,
) -> DocumentArtifact:
    """`put_bytes` без буферизации: шифрует порции по мере поступления.

    Нужен загрузке аудио сессии (docs/18 §4.6): 200 МБ в память не читаются,
    шифротекст пишется в файл сразу. Пока порции идут, на диске лежит только
    `.tmp` с шифротекстом; при любом сбое он удаляется.
    """
    if kind not in KINDS:
        raise Problem(422, "ARTIFACT_KIND_UNKNOWN", f"Неизвестный вид артефакта: {kind}")

    root = _root()
    rel = f"{kind}/{uuid.uuid4().hex}.bin"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    nonce = os.urandom(NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(_key()), modes.GCM(nonce)).encryptor()
    digest = hashlib.sha256()
    size = 0

    def _write(fh, blob: bytes) -> None:
        nonlocal size
        fh.write(blob)
        digest.update(blob)
        size += len(blob)

    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            _write(fh, MAGIC + nonce)
            for chunk in chunks:
                if chunk:
                    _write(fh, encryptor.update(chunk))
            _write(fh, encryptor.finalize())
            _write(fh, encryptor.tag)
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
        sha256=digest.hexdigest(),
        size_bytes=size,
        mime=mime,
        encrypted=True,
    )


def _artifact_path(artifact: DocumentArtifact) -> Path:
    root = _root().resolve()
    path = (root / artifact.path).resolve()
    if root not in path.parents:
        raise Problem(500, "ARTIFACT_CORRUPT", "Путь артефакта вне CC_ARTIFACTS_DIR")
    return path


def iter_plaintext(artifact: DocumentArtifact) -> Iterator[bytes]:
    """Расшифровать артефакт порциями (по `CHUNK_BYTES`), не читая файл целиком.

    Хэш сверяется по шифротексту на лету; тег GCM проверяется при `finalize`,
    т.е. до этого места вызывающий уже записал часть открытого текста. Для
    аудио сессии это допустимо: при провале тега вызывающий удаляет свой
    временный файл (см. `decrypt_to_temp`).
    """
    path = _artifact_path(artifact)
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        raise Problem(500, "ARTIFACT_MISSING", "Файл артефакта отсутствует")
    with handle:
        head = handle.read(len(MAGIC) + NONCE_BYTES)
        if len(head) < len(MAGIC) + NONCE_BYTES:
            raise Problem(500, "ARTIFACT_CORRUPT", "Артефакт обрезан")
        if not head.startswith(MAGIC):
            raise Problem(500, "ARTIFACT_CORRUPT", "Неизвестный формат артефакта")
        nonce = head[len(MAGIC) : len(MAGIC) + NONCE_BYTES]
        total = handle.seek(0, os.SEEK_END)
        body_bytes = total - len(head) - TAG_BYTES
        if body_bytes < 0:
            raise Problem(500, "ARTIFACT_CORRUPT", "Артефакт обрезан")
        # GCM-тег — в конце файла; хэш считается в файловом порядке байт.
        handle.seek(total - TAG_BYTES)
        tag = handle.read(TAG_BYTES)
        handle.seek(len(head))
        decryptor = Cipher(algorithms.AES(_key()), modes.GCM(nonce, tag)).decryptor()
        digest = hashlib.sha256(head)

        remaining = body_bytes
        while remaining > 0:
            chunk = handle.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                raise Problem(500, "ARTIFACT_CORRUPT", "Артефакт обрезан")
            remaining -= len(chunk)
            digest.update(chunk)
            plain = decryptor.update(chunk)
            if plain:
                yield plain
        digest.update(tag)
        if digest.hexdigest() != artifact.sha256:
            raise Problem(
                500, "ARTIFACT_CORRUPT", "Хэш артефакта не совпадает с метаданными"
            )
        try:
            decryptor.finalize()
        except InvalidTag:
            raise Problem(500, "ARTIFACT_CORRUPT", "Артефакт повреждён или подменён") from None


def decrypt_to_temp(artifact: DocumentArtifact) -> Path:
    """Расшифровать артефакт в файл 0600 под `CC_ARTIFACTS_DIR/tmp`.

    Открытый текст нужен внешнему потребителю (ASR-сайдкар принимает файл), но
    в память целиком не поднимается. Файл обязательно удаляет вызывающий —
    окно открытого текста на диске ограничено временем запроса.
    """
    tmp_dir = _root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_dir / f"{uuid.uuid4().hex}.plain"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            for chunk in iter_plaintext(artifact):
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def temp_dir() -> Path:
    """Каталог временных файлов расшифровки (для проверок и уборки)."""
    return _root() / "tmp"


def sweep_orphans(known: set[str]) -> int:
    """Удалить файлы артефактов, на которые нет строки в БД (старт приложения).

    Строка и файл пишутся в разных шагах: отмена между `os.replace` в
    `put_stream` и коммитом оставляет шифротекст без ссылки, а такой файл
    живёт вне ретенции (docs/18 §4.6). Свип только на старте — в этот момент
    незавершённых загрузок нет. `known` — множество относительных путей из
    `document_artifacts.path` (имя файла не совпадает с id артефакта).
    """
    removed = 0
    root = _root()
    for kind in KINDS:
        directory = root / kind
        if not directory.is_dir():
            continue
        for path in directory.glob("*.bin"):
            if f"{kind}/{path.name}" in known:
                continue
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def purge_temp(*, max_age_sec: float) -> int:
    """Удалить остатки открытого текста старше `max_age_sec` из `tmp`.

    Штатно файл удаляет `_pipeline` в `finally`, но убитый процесс оставить его
    мог: открытый текст аудио не должен переживать рестарт. Чужие файлы не
    трогаем — при `WEB_CONCURRENCY>1` соседний воркер в этот момент кормит
    сайдкар из своего файла, поэтому порог задаёт вызывающий
    (`CC_SESSION_AI_TIMEOUT × 2`).
    """
    cutoff = time.time() - max_age_sec
    removed = 0
    for path in temp_dir().glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except FileNotFoundError:  # файл убрал сам воркер
            continue
    return removed


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
