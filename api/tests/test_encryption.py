"""F1: проверка шифрования free-text ПДн в покое (pgcrypto).

Сырой SELECT колонки возвращает bytea (не читаемый текст) — ПДн зашифрован на диске.
ORM-чтение через EncryptedText возвращает расшифрованный plaintext.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.models import ChatMessage, DiaryEntry, User


def _patient(db_session) -> uuid.UUID:
    u = User(email=f"p-enc-{uuid.uuid4().hex[:8]}@test.ru", role="PATIENT", password_hash="x")
    db_session.add(u)
    db_session.flush()
    return u.id


def test_free_text_encrypted_at_rest(db_session):
    """Дневник: в БД лежит bytea, через ORM читается plaintext."""
    pid = _patient(db_session)
    db_session.add(DiaryEntry(patient_id=pid, content="секретный текст дневника", mood=3))
    db_session.commit()
    # сырой SQL в обход ORM — колонка bytea, plaintext отсутствует
    row = db_session.execute(
        text("SELECT content::text AS c FROM diary_entries WHERE patient_id = CAST(:p AS uuid) LIMIT 1").bindparams(p=str(pid))
    ).one()
    assert "секретный текст дневника" not in row.c  # ПДн НЕ в открытом виде на диске
    # ORM-чтение расшифровывает
    de = db_session.execute(select(DiaryEntry).where(DiaryEntry.patient_id == pid)).scalar_one()
    assert de.content == "секретный текст дневника"


def test_chat_message_encrypted_at_rest(db_session):
    pid = _patient(db_session)
    db_session.add(ChatMessage(patient_id=pid, sender="PATIENT", kind="TEXT", content="тревожно ночью"))
    db_session.commit()
    row = db_session.execute(
        text("SELECT content::text AS c FROM chat_messages WHERE patient_id = CAST(:p AS uuid) LIMIT 1").bindparams(p=str(pid))
    ).one()
    assert "тревожно ночью" not in row.c  # не plaintext в покое
    msg = db_session.execute(select(ChatMessage).where(ChatMessage.patient_id == pid)).scalar_one()
    assert msg.content == "тревожно ночью"  # ORM расшифровал
