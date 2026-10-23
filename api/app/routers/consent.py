# E3: consent. Контракт: docs/04 §3.1; тесты — docs/06 §3.
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_patient
from app.errors import Problem
from app.models import Consent, User

router = APIRouter(prefix="/consent", tags=["consent"])


class ConsentBody(BaseModel):
    version: int = Field(ge=1)


async def _active(db: AsyncSession, user_id) -> Consent | None:
    return await db.scalar(select(Consent).where(Consent.user_id == user_id, Consent.revoked_at.is_(None)))


@router.post("", status_code=201)
async def create(body: ConsentBody, user: User = Depends(require_patient), db: AsyncSession = Depends(get_db)):
    if await _active(db, user.id):
        raise Problem(409, "CONFLICT", "Активное согласие уже подписано")
    consent = Consent(user_id=user.id, version=body.version, text_hash=f"consent-v{body.version}")
    db.add(consent)
    await db.commit()
    return {"id": str(consent.id), "version": consent.version, "revoked_at": consent.revoked_at}


@router.get("")
async def get_active(user: User = Depends(require_patient), db: AsyncSession = Depends(get_db)):
    consent = await _active(db, user.id)
    if consent is None:
        raise Problem(404, "NOT_FOUND", "Активное согласие не найдено")
    return {
        "id": str(consent.id),
        "version": consent.version,
        "signed_at": consent.signed_at.isoformat(),
        "revoked_at": consent.revoked_at,
    }


@router.post("/revoke", status_code=204)
async def revoke(user: User = Depends(require_patient), db: AsyncSession = Depends(get_db)):
    consent = await _active(db, user.id)
    if consent is None:
        raise Problem(409, "CONFLICT", "Нет активного согласия для отзыва")
    consent.revoked_at = datetime.now(timezone.utc)
    await _destroy_patient_pdn(db, user.id)
    await db.commit()


async def _destroy_patient_pdn(db: AsyncSession, patient_id) -> None:
    """F2 (152-ФЗ ст.9 ч.2-3): уничтожение спецкатегории ПДн при отзыве согласия.

    DELETE строк пациента в FK-безопасном порядке (дети раньше родителей):
    notification → escalation → answer → assessment → run, и независимые
    diary/chat/adverse/adherence/visit. Свободный текст был зашифрован pgcrypto —
    удаление строк уничтожает зашифрованные blob'ы. Consent + audit_log остаются
    (реквизиты ст. 9 ч.4 и WORM-аудит доступа — основание обработки, не уничтожаются).
    Visit — запись приёма врача с зашифрованным notes (спецкатегория ПДн): после
    отзыва согласия врач не вправе читать заметки беседы, поэтому строка уничтожается
    вместе с остальной клинической спецкатегорией (152-ФЗ ст.9 ч.2-3, инвариант F2).
    PushSubscription/DeviceToken — аутентификационно-связанные каналы доставки
    (endpoint/keys/FCM-токен), привязанные к user_id; при отзыве согласия
    уничтожаются тоже, иначе отозванный пациент продолжает получать push
    (утечка канала доставки 152-ФЗ ст.9 ч.2-3). Prescription/ClinicianRoster —
    операционные связи, остаются (врач может повторно назначить при ре-консенте).
    Реальная деструкция 30 дней выполняется шедулером в проде; в MVP — немедленно.
    """
    from sqlalchemy import delete as sa_delete, select as sa_select, update as sa_update

    from app.models import (
        AdherenceRecord,
        AdverseEvent,
        Answer,
        Assessment,
        CheckinSession,
        ChatMessage,
        DeviceToken,
        DiaryEntry,
        EmiasLink,
        Escalation,
        ClinicianReminder,
        Notification,
        PatientMedication,
        PushSubscription,
        Run,
        User,
        Visit,
    )

    # дети → родители. Answer не имеет patient_id — удаляем через подзапрос по runs.
    await db.execute(sa_delete(Notification).where(Notification.patient_id == patient_id))
    # FR-24: напоминания врача по пациенту (note — свободный текст, может содержать
    # ФИО/состояние). Удаляются после notifications (FK notifications.reminder_id)
    # и до остальных сущностей: после отзыва согласия врач не вправе их читать.
    await db.execute(sa_delete(ClinicianReminder).where(ClinicianReminder.patient_id == patient_id))
    await db.execute(sa_delete(Escalation).where(Escalation.patient_id == patient_id))
    # docs/16 §5: ответы чек-ина — спецкатегория ПДн; после эскалаций (FK
    # escalations.checkin_session_id) и до удаления пользователя.
    await db.execute(sa_delete(CheckinSession).where(CheckinSession.patient_id == patient_id))
    run_ids = sa_select(Run.id).where(Run.patient_id == patient_id)
    await db.execute(sa_delete(Answer).where(Answer.run_id.in_(run_ids)))
    await db.execute(sa_delete(Assessment).where(Assessment.patient_id == patient_id))
    await db.execute(sa_delete(Run).where(Run.patient_id == patient_id))
    # независимые
    await db.execute(sa_delete(DiaryEntry).where(DiaryEntry.patient_id == patient_id))
    await db.execute(sa_delete(ChatMessage).where(ChatMessage.patient_id == patient_id))
    await db.execute(sa_delete(AdverseEvent).where(AdverseEvent.patient_id == patient_id))
    await db.execute(sa_delete(AdherenceRecord).where(AdherenceRecord.patient_id == patient_id))
    # F2 (re-audit): каналы доставки push — уничтожаем, иначе отозванный пациент
    # продолжает получать уведомления (утечка канала, 152-ФЗ ст.9 ч.2-3).
    await db.execute(sa_delete(PushSubscription).where(PushSubscription.user_id == patient_id))
    await db.execute(sa_delete(DeviceToken).where(DeviceToken.user_id == patient_id))
    # FR-25: история назначений ЕМИАС (title/dosage косвенно указывают на диагноз)
    # и связка (snils_hash) — спецкатегория ПДн; без явного удаления переживали
    # отзыв согласия (F2, 152-ФЗ ст.9 ч.2-3).
    await db.execute(sa_delete(PatientMedication).where(PatientMedication.patient_id == patient_id))
    await db.execute(sa_delete(EmiasLink).where(EmiasLink.patient_id == patient_id))
    # Visit: зашифрованные заметки беседы врача — та же спецкатегория ПДн (F2).
    await db.execute(sa_delete(Visit).where(Visit.patient_id == patient_id))
    # FR-22: контакты пациента (телефон — ПДн, дата рождения) уничтожаются вместе
    # с остальной спецкатегорией. Учётную запись удалять нельзя (аудит) → обнуляем.
    await db.execute(sa_update(User).where(User.id == patient_id).values(phone=None, birth_date=None))
    await db.flush()