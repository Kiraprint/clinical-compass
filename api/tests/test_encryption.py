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


def test_adherence_medication_encrypted_at_rest(db_session):
    """F1 round-3: AdherenceRecord.medication (название препарата = контекст лечения)
    зашифрован at rest; ORM читает plaintext."""
    from sqlalchemy import text as sql_text

    from app.models import AdherenceRecord, User
    import uuid

    uid = uuid.uuid4()
    db_session.add(User(id=uid, email=f"med-{uid}@t.ru", password_hash="x", role="PATIENT"))
    db_session.flush()
    db_session.add(AdherenceRecord(patient_id=uid, medication="Авиандр", status="taken"))
    db_session.commit()

    row = db_session.execute(
        sql_text("SELECT medication::text AS c FROM adherence_records WHERE patient_id = CAST(:p AS uuid)"),
        {"p": str(uid)},
    ).first()
    assert row is not None
    assert "Авиандр" not in (row.c or ""), "medication должен быть зашифрован at rest"

    rec = db_session.query(AdherenceRecord).filter(AdherenceRecord.patient_id == uid).one()
    assert rec.medication == "Авиандр"
