"""ORM-модели. Схема — docs/04-спецификация.md §5. Спецкатегории ПДн (152-ФЗ)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Identity, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .crypto import EncryptedText
from .db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role in ('PATIENT','CLINICIAN','ADMIN')", name="users_role"),
        CheckConstraint("role = 'PATIENT' or totp_secret is not null", name="users_totp_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    fio: Mapped[str | None] = mapped_column(String, nullable=True)
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)  # PATIENT | CLINICIAN | ADMIN
    totp_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    # Шкалы скрининга назначаются врачом (roster) через таблицу prescriptions —
    # пациент выбирает только из назначенного множества (см. services/screening.py).
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column()
    text_hash: Mapped[str] = mapped_column(String)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("consents_active", "user_id", unique=True, postgresql_where=text("revoked_at is null")),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("audit_log_occurred", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ip: Mapped[str | None] = mapped_column(String, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    questionnaire_code: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String)  # SCREENING | CHECKIN | FOLLOW_UP
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("mode in ('SCREENING','CHECKIN','FOLLOW_UP','CLINICIAN')", name="runs_mode"),
        Index(
            "runs_screening_open",
            "patient_id",
            unique=True,
            postgresql_where=text("mode = 'SCREENING' AND finished_at IS NULL"),
        ),
        Index(
            "runs_checkin_daily",
            "patient_id",
            text("((started_at AT TIME ZONE 'Europe/Moscow')::date)"),
            unique=True,
            postgresql_where=text("mode = 'CHECKIN'"),
        ),
    )


class Answer(Base):
    __tablename__ = "answers"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    item_key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int]
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class Assessment(Base):
    __tablename__ = "assessments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), unique=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    total_score: Mapped[int]
    severity: Mapped[str] = mapped_column(String)
    triage: Mapped[str] = mapped_column(String)
    risk_flags: Mapped[list] = mapped_column(JSONB, default=list)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), index=True)

    __table_args__ = (Index("assessments_patient_time", "patient_id", "computed_at"),)


class Escalation(Base):
    __tablename__ = "escalations"
    __table_args__ = (Index("escalations_patient", "patient_id", "resolved_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    assessment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assessments.id"))
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String)  # SUICIDAL | SEVERE
    notified_clinician_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)


class DiaryEntry(Base):
    __tablename__ = "diary_entries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    content: Mapped[str] = mapped_column(EncryptedText())
    mood: Mapped[int | None] = mapped_column(nullable=True)  # 1..10
    sleep_h: Mapped[float | None] = mapped_column(nullable=True)
    # NEW-2: теги — свободный текст пациента (ПДн). В БД лежит зашифрованная
    # JSON-строка; наружу отдаётся list[str] через property ниже.
    tags_json: Mapped[str] = mapped_column("tags", EncryptedText, default="[]")

    @property
    def tags(self) -> list[str]:
        if not self.tags_json:
            return []
        try:
            return json.loads(self.tags_json)
        except (ValueError, TypeError):
            return []

    @tags.setter
    def tags(self, value) -> None:
        # NOT NULL колонка: пустой список хранится как "[]", не NULL.
        self.tags_json = json.dumps(list(value or []), ensure_ascii=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), index=True)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("sender in ('PATIENT','SYSTEM','CLINICIAN')", name="chat_sender"),
        CheckConstraint("kind in ('TEXT','FAQ','HOTLINE','LLM')", name="chat_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    no: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)  # детерминированный порядок
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    sender: Mapped[str] = mapped_column(String)  # PATIENT | SYSTEM
    kind: Mapped[str] = mapped_column(String)  # TEXT | FAQ | HOTLINE
    content: Mapped[str] = mapped_column(EncryptedText())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), index=True)


class ClinicianRoster(Base):
    __tablename__ = "clinician_roster"

    clinician_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

class Prescription(Base):
    """Назначенные пациенту шкалы скрининга (множество). PK (patient_id, scale_code).
    Кто назначил — в audit_log (PRESCRIBE_SCALE); строка хранит только сам набор."""
    __tablename__ = "prescriptions"

    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    scale_code: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    endpoint: Mapped[str] = mapped_column(String, unique=True)
    p256dh: Mapped[str] = mapped_column(String)
    auth: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String)  # RED_ESCALATION
    title: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(String)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    escalation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("escalations.id"), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), index=True)

    __table_args__ = (
        CheckConstraint("kind in ('RED_ESCALATION')", name="notifications_kind"),
        Index("notifications_user_read", "user_id", "read_at"),
    )

class DeviceToken(Base):
    """FCM-токен нативного приложения (Android APK, item 4). Канал: FCM HTTP v1."""

    __tablename__ = "device_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token: Mapped[str] = mapped_column(String, unique=True)
    platform: Mapped[str] = mapped_column(String)  # android | ios | web
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("platform in ('android','ios','web')", name="device_tokens_platform"),
    )

class FcmState(Base):
    """Одноразовый переход на FCM-канал нативных пушей (item 4).

    Cooldown: после успешного апгрейда повторная подписка возвращает
    429 FCM_COOLDOWN в течение окна (60 мин), чтобы клиент показал
    «Попробуйте через час» вместо мгновенного повторного сабскрайба.
    """

    __tablename__ = "fcm_state"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    last_upgraded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdverseEvent(Base):
    """Нежелательное явление (задача кейса 3). Спецкатегория ПДн (152-ФЗ).

    Вносит врач (clinician_id) в карточку пациента: событие, тяжесть, предполагаемый
    препарат (напр. «Авиандр»), дата начала, предпринятое действие. Учёт рисков и НЯ
    терапии — для мониторинга безопасности (обязательное условие: предупреждения о
    безопасности терапии визуально выделены, связка с P6 routing в UI).
    """

    __tablename__ = "adverse_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    clinician_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    event_text: Mapped[str] = mapped_column(EncryptedText())
    severity: Mapped[str] = mapped_column(String(16))   # mild | moderate | severe
    onset_date: Mapped[datetime] = mapped_column(Date)
    suspected_drug: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)  # NEW-3: контекст лечения
    action_taken: Mapped[str | None] = mapped_column(String(32))  # none|monitoring|dose_reduced|stopped|referred
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), index=True)

    __table_args__ = (
        CheckConstraint("severity in ('mild','moderate','severe')", name="adverse_events_severity"),
        CheckConstraint(
            "action_taken is null or action_taken in ('none','monitoring','dose_reduced','stopped','referred')",
            name="adverse_events_action",
        ),
    )


class AdherenceRecord(Base):
    """Приверженность лечению (задача кейса 7). Спецкатегория ПДн (152-ФЗ).

    Пациент ежедневно отмечает приём назначенного препарата: taken/partial/missed.
    Врач видит агрегат (% приверженности за окно) в карточке. medication — свободная
    строка (напр. «Авиандр»), без FK на модель назначения (YAGNI для демо).
    """

    __tablename__ = "adherence_records"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    medication: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)  # F1: контекст лечения
    status: Mapped[str] = mapped_column(String(16))   # taken | partial | missed
    note: Mapped[str | None] = mapped_column(EncryptedText())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), index=True)

    __table_args__ = (
        CheckConstraint("status in ('taken','partial','missed')", name="adherence_records_status"),
    )


class Visit(Base):
    """Приём: запись очной/дистанционной беседы врача с пациентом (задача кейса 6,
    п. «приём» в карточке пациента). Спецкатегория ПДн (152-ФЗ) — notes шифруются.

    Вносит врач (clinician_id): дата приёма, формат (in_person/online/phone),
    длительность в минутах и свободная заметка. История приёмов видна в карточке —
    врач отслеживает динамику общения между скринингами.
    """

    __tablename__ = "visits"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    clinician_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    visited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    visit_type: Mapped[str] = mapped_column(String(16))  # in_person | online | phone
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(EncryptedText())  # F1: запись беседы
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("visit_type in ('in_person','online','phone')", name="visits_type"),
        CheckConstraint("duration_min is null or (duration_min between 1 and 480)", name="visits_duration"),
    )


class Drug(Base):
    """Препараты (источник — ГРЛС, grls.rosminzdrav.ru). Справочник для ИИ-агента
    (чанки в Qdrant строятся из этой таблицы — api/app/kb_seed._drug_docs) и для
    API врач/админ. Не ПДн: публичные факты регистрационного удостоверения."""

    __tablename__ = "drugs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    drug_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # slug «aviandr»
    trade_name: Mapped[str] = mapped_column(String(128))
    inn: Mapped[str] = mapped_column(String(128))
    inn_en: Mapped[str | None] = mapped_column(String(128), nullable=True)
    form: Mapped[str] = mapped_column(String(255))
    reg_certificate: Mapped[str] = mapped_column(String(128))
    reg_date: Mapped[datetime] = mapped_column(Date)
    revision_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    pharmacology: Mapped[str] = mapped_column(Text)
    indications: Mapped[str] = mapped_column(Text)
    dosage: Mapped[str] = mapped_column(Text)
    contraindications: Mapped[str] = mapped_column(Text)
    adverse_events: Mapped[str] = mapped_column(Text)
    special_notes: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class RefreshToken(Base):
    """F4 (security-audit): server-side rotating refresh tokens с jti-стором.

    Заменяет stateless JWT refresh — каждый логин/ротация создаёт строку, /refresh
    проверяет jti + revoked + expires, ротирует (revoke old → issue new), /auth/logout
    отзывает. Украденный refresh живёт до первого использования легитимным клиентом.
    """
    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("refresh_tokens_user", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)