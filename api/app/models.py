"""ORM-модели. Схема — docs/04-спецификация.md §5. Спецкатегории ПДн (152-ФЗ)."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, CHAR, CheckConstraint, Date, DateTime, ForeignKey, Float, Identity, Index, Integer, LargeBinary, String, Text, UniqueConstraint, text
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
    # FR-22: контакты пациента. Телефон — ПДн, шифруется (F1, EncryptedText).
    # Дата рождения — календарная дата без текста, хранится открыто: шифрование
    # не даёт выигрыша (энтропия даты ~ диапазон лет), доступ закрыт roster/consent
    # (обоснование — docs/02 §5).
    phone: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
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
        # runs_checkin_daily удалён (миграция c6d7e8f9a0b1): уникальность дня
        # обеспечивает checkin_sessions(patient_id, checkin_date).
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


class CheckinSession(Base):
    """Ежедневный адаптивный чек-ин (docs/16 §5). Ответы — спецкатегория ПДн."""

    __tablename__ = "checkin_sessions"
    __table_args__ = (UniqueConstraint("patient_id", "checkin_date", name="checkin_sessions_daily"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    checkin_date: Mapped[date] = mapped_column(Date)  # день по Москве
    rules_version: Mapped[str] = mapped_column(String(16))
    # F1: ответы и предъявленные пункты — медицинские данные; в БД только
    # зашифрованная JSON-строка (как DiaryEntry.tags_json), наружу — dict.
    answers_json: Mapped[str] = mapped_column("answers", EncryptedText, default="{}")
    presented_json: Mapped[str] = mapped_column("presented", EncryptedText, default="{}")
    phase: Mapped[str] = mapped_column(String(16))  # core | safety | modules | done
    status: Mapped[str] = mapped_column(String(16))  # in_progress | completed
    safety: Mapped[str] = mapped_column(String(16))  # none | signal | review | unassessed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def answers(self) -> dict[str, int]:
        return json.loads(self.answers_json or "{}")

    @answers.setter
    def answers(self, value: dict[str, int]) -> None:
        self.answers_json = json.dumps(value, ensure_ascii=False, sort_keys=True)

    @property
    def presented(self) -> dict:
        return json.loads(self.presented_json or "{}")

    @presented.setter
    def presented(self, value: dict) -> None:
        self.presented_json = json.dumps(value, ensure_ascii=False, sort_keys=True)


class Escalation(Base):
    __tablename__ = "escalations"
    __table_args__ = (
        Index("escalations_patient", "patient_id", "resolved_at"),
        # Одна эскалация на сеанс чек-ина: partial unique (docs/16 §3).
        Index(
            "escalations_checkin_session",
            "checkin_session_id",
            unique=True,
            postgresql_where=text("checkin_session_id IS NOT NULL"),
        ),
        # Очередь открытых сигналов: `resolved_at IS NULL`, сортировка
        # `created_at DESC NULLS LAST` (created_at nullable) — тот же индекс,
        # что создаёт миграция e5f6a7b8c9d0.
        Index(
            "escalations_open_queue",
            text("created_at DESC NULLS LAST"),
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Либо скрининг, либо чек-ин: источник эскалации (CHECKIN_RISK идёт без assessment).
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessments.id"), nullable=True
    )
    checkin_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("checkin_sessions.id"), nullable=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String)  # SUICIDAL | SEVERE | CHECKIN_RISK | CHECKIN_RISK_URGENT
    notified_clinician_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # server_default now() заполняет и старые строки временем миграции — NULL не остаётся.
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)


class DiaryEntry(Base):
    __tablename__ = "diary_entries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    content: Mapped[str] = mapped_column(EncryptedText())
    mood: Mapped[int | None] = mapped_column(nullable=True)  # 0..4 при mood_scale='0-4'; legacy NULL = 1..10
    # Шкала настроения записи: '0-4' для новых, NULL — историчные 1..10 (не переинтерпретируем).
    mood_scale: Mapped[str | None] = mapped_column(String(8), nullable=True)
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

class ClinicianReminder(Base):
    """FR-24: личное напоминание врача по пациенту (позвонить/написать/приём/своё).

    note — свободный текст, может содержать ФИО/состояние (спецкатегория ПДн),
    поэтому EncryptedText (F1). due_at — момент, когда напоминание пора доставить;
    notified_at ставит воркер при создании in-app Notification (идемпотентность).
    Все выборки сервиса ограничены clinician_id владельца.
    """

    __tablename__ = "clinician_reminders"
    __table_args__ = (
        CheckConstraint(
            "kind in ('call','write','appointment','custom')", name="clinician_reminders_kind"
        ),
        CheckConstraint(
            "status in ('open','done','cancelled')", name="clinician_reminders_status"
        ),
        Index("clinician_reminders_clinician_due", "clinician_id", "status", "due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    clinician_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String, default="open")
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

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
    kind: Mapped[str] = mapped_column(String)  # RED_ESCALATION | REMINDER | ADVERSE_EVENT
    title: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(String)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    escalation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("escalations.id"), nullable=True)
    # FR-24: ключ дедупликации напоминания — один Notification на напоминание
    # (уникальный индекс). Повторный тик воркера не плодит уведомления.
    # CASCADE — удаление напоминания (отзыв согласия) не должно упираться в FK.
    reminder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clinician_reminders.id", ondelete="CASCADE"), nullable=True, unique=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), index=True)

    __table_args__ = (
        # ADVERSE_EVENT — FR-28: серьёзное НЯ, о котором сообщил пациент.
        CheckConstraint("kind in ('RED_ESCALATION','REMINDER','ADVERSE_EVENT')", name="notifications_kind"),
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
    """Нежелательное явление (FR-28). Спецкатегория ПДн (152-ФЗ).

    Два источника:
      * patient — пациент сообщает о НЯ сам (status='pending', clinician_id NULL).
        Врач закреплённого ростера подтверждает, отклоняет или корректирует запись
        (verified_by/verified_at/verification_note).
      * clinician — врач вносит НЯ в карточку: сразу confirmed, clinician_id автора
        (историческое поведение до FR-28).
    drug_id/medication_id связывают событие со справочником ГРЛС (текст побочек
    доступен врачу) и с конкретным назначением пациента.
    """

    __tablename__ = "adverse_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    clinician_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    event_text: Mapped[str] = mapped_column(EncryptedText())
    # FR-33/34: пациент тяжесть не оценивает — severity NULL до проверки врачом
    # (mild | moderate | severe выставляет врач). У врачебных/исторических строк заполнено.
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    onset_date: Mapped[datetime] = mapped_column(Date)
    suspected_drug: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)  # NEW-3: контекст лечения
    action_taken: Mapped[str | None] = mapped_column(String(32))  # none|monitoring|dose_reduced|stopped|referred
    # FR-28: кто сообщил и состояние проверки. У исторических строк —
    # clinician/confirmed (server_default миграции).
    reported_by: Mapped[str] = mapped_column(String(16), server_default=text("'clinician'"))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'confirmed'"))
    drug_id: Mapped[str | None] = mapped_column(
        ForeignKey("drugs.drug_id"), nullable=True, index=True
    )  # справочник ГРЛС
    medication_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("patient_medications.id", ondelete="SET NULL"), nullable=True
    )
    verified_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_note: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), index=True)

    __table_args__ = (
        CheckConstraint("severity in ('mild','moderate','severe')", name="adverse_events_severity"),
        CheckConstraint(
            "action_taken is null or action_taken in ('none','monitoring','dose_reduced','stopped','referred')",
            name="adverse_events_action",
        ),
        CheckConstraint("reported_by in ('clinician','patient')", name="adverse_events_reported_by"),
        CheckConstraint("status in ('pending','confirmed','rejected')", name="adverse_events_status"),
        # Очередь врача: pending, свежие сверху.
        Index("ix_adverse_events_status_created", "status", text("created_at desc")),
    )


class AdherenceRecord(Base):
    """Приверженность лечению (задача кейса 7). Спецкатегория ПДн (152-ФЗ).

    Пациент ежедневно отмечает приём назначенного препарата: taken/partial/missed
    либо not_scheduled (сегодня не назначено). У «частично» уточняется
    partial_kind — сколько принято. Врач видит агрегат (% приверженности за
    окно) в карточке. medication_id —
    FK на назначение (FR-27): по нему считается ожидаемое число приёмов из
    режима (PatientMedication.times_per_day). medication — свободный текст
    названия для отображения и для исторических записей без связи (backfill
    не делался).
    """

    __tablename__ = "adherence_records"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    medication: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)  # F1: контекст лечения
    # FR-27: связь с назначением. При удалении назначения отметка остаётся без
    # связи (SET NULL) и выпадает из знаменателя — история пациента не теряется.
    medication_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("patient_medications.id", ondelete="SET NULL"), index=True, nullable=True,
    )
    # taken | partial | missed | not_scheduled. not_scheduled — препарат сегодня
    # не принимается по расписанию (перерыв/по требованию): в числитель и
    # знаменатель процента не входит, показывается отдельным счётчиком.
    status: Mapped[str] = mapped_column(String(16))
    # Уточнение «частичного» приёма (жалоба врача-психиатра): сколько именно
    # принято. Обязателен при status='partial' и запрещён при остальных
    # статусах — инвариант держат CHECK-констрейнты ниже.
    partial_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str | None] = mapped_column(EncryptedText())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), index=True)

    __table_args__ = (
        CheckConstraint(
            "status in ('taken','partial','missed','not_scheduled')",
            name="adherence_records_status",
        ),
        CheckConstraint(
            "partial_kind is null or partial_kind in ("
            "'quarter','half','three_quarters','one_of_two','two_of_three',"
            "'less_than_prescribed','self_changed')",
            name="adherence_records_partial_kind",
        ),
        # kind только для partial и обязателен для него.
        CheckConstraint(
            "(status = 'partial') = (partial_kind is not null)",
            name="adherence_records_partial_kind_required",
        ),
    )


class Visit(Base):
    """Приём: запись очной/дистанционной беседы врача с пациентом (задача кейса 6,
    п. «приём» в карточке пациента). Спецкатегория ПДн (152-ФЗ) — notes шифруются.

    Вносит врач (clinician_id): дата приёма, формат (in_person/online/phone),
    длительность в минутах и свободная заметка. История приёмов видна в карточке —
    врач отслеживает динамику общения между скринингами.
    FR-25: приёмы, импортированные из ЕМИАС, живут в этой же таблице
    (source='emias', clinician_id = NULL, emias_id — для идемпотентного upsert).
    """

    __tablename__ = "visits"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    clinician_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    visited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    visit_type: Mapped[str] = mapped_column(String(16))  # in_person | online | phone
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(EncryptedText())  # F1: запись беседы
    source: Mapped[str] = mapped_column(String(8), server_default=text("'local'"))  # local | emias
    emias_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    # docs/18 §3.3: приём, созданный завершением клинической сессии (FR-38/39).
    # UNIQUE — идемпотентность finish: повторный вызов не плодит второй приём.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clinical_sessions.id"), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("visit_type in ('in_person','online','phone')", name="visits_type"),
        CheckConstraint("duration_min is null or (duration_min between 1 and 480)", name="visits_duration"),
        CheckConstraint("source in ('local','emias')", name="visits_source"),
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
    # Провенанс: URL записей ГРЛС и инструкций, по которым заполнены поля.
    # Пустой список — источник известен только на уровне файла drugs.json.
    source_urls: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
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


class KBWork(Base):
    """Единица внешнего корпуса (статья PubMed / гайдлайн ВОЗ) — реестр источников.

    Qdrant — производный индекс: чанки пишутся только из этой таблицы
    (app/enrichment/index.py), поэтому источник истины и провенанс — здесь.
    Статусы: candidate (ждёт врача) → approved (в индексе) | rejected;
    guideline публикуется сразу, superseded — версия заменена новой редакцией,
    прежние чанки удалены из Qdrant.
    """

    __tablename__ = "kb_works"
    __table_args__ = (
        CheckConstraint("kind in ('article','guideline')", name="kb_works_kind"),
        CheckConstraint(
            "status in ('candidate','approved','rejected','excluded','superseded')",
            name="kb_works_status",
        ),
        UniqueConstraint("source", "external_id", name="kb_works_source_external"),
        UniqueConstraint("guideline_id", name="kb_works_guideline_id"),
        Index("ix_kb_works_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16))  # article | guideline
    source: Mapped[str] = mapped_column(String(16))  # pubmed | who
    external_id: Mapped[str] = mapped_column(String(64))  # PMID | IRIS handle
    # Идентификатор чанков в Qdrant: pmid-123 / who-10665-374250.
    guideline_id: Mapped[str] = mapped_column(String(96))
    revision: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="candidate")
    title: Mapped[str] = mapped_column(Text)
    # Полный текст есть только у гайдлайнов ВОЗ (TEXT bitstream IRIS); статьи
    # хранят абстракт в meta. Отдельная колонка — чтобы не раздувать meta,
    # которую отдают API-эндпоинты ревью.
    full_text: Mapped[str] = mapped_column(Text, default="")
    # journal/year/authors/doi/pmid/url/license/abstract/results/publication_types/
    # language/fulltext_url — структурированные метаданные источника.
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    design: Mapped[dict] = mapped_column(JSONB, default=dict)
    criteria: Mapped[dict] = mapped_column(JSONB, default=dict)
    appraisal: Mapped[dict] = mapped_column(JSONB, default=dict)
    retraction: Mapped[str] = mapped_column(String(16), default="unknown")
    revision_history: Mapped[list] = mapped_column(JSONB, default=list)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    indexed_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chunk_count: Mapped[int] = mapped_column(default=0)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class KBRun(Base):
    """Прогон обогащения КБ: воспроизводимость поиска (docs/15).

    query_plan — фактические запросы по каждой базе, versions — версии алгоритмов,
    схем критериев/оценки и эмбеддера; stats/errors — что найдено и что упало.
    """

    __tablename__ = "kb_runs"
    __table_args__ = (
        CheckConstraint("status in ('running','ok','partial','failed')", name="kb_runs_status"),
        Index("ix_kb_runs_started_at", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    trigger: Mapped[str] = mapped_column(String(16))  # schedule | manual | cli
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    query_plan: Mapped[dict] = mapped_column(JSONB, default=dict)
    versions: Mapped[dict] = mapped_column(JSONB, default=dict)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    errors: Mapped[list] = mapped_column(JSONB, default=list)


class KBExclusion(Base):
    """Персональное исключение источника врачом: источник остаётся в КБ, но
    не попадает в выдачу этого пользователя (фильтр retrieval, docs/15 §6)."""

    __tablename__ = "kb_exclusions"
    __table_args__ = (UniqueConstraint("user_id", "guideline_id", name="kb_exclusions_user_guideline"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    guideline_id: Mapped[str] = mapped_column(String(96))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class KBWorkApproval(Base):
    """FR-36: персональное решение врача по статье — видимость в его выдаче.

    `kb_works.status='approved'` — глобальная публикация в индекс админом;
    эта таблица решает, видит ли конкретный врач статью в ответах ассистента.
    Записи нет → статья врачу не видна (гайдлайны ВОЗ видны всем: kind='guideline').
    Своё решение врач может поменять (approved ↔ rejected) — строка обновляется,
    reviewed_at фиксирует последнее изменение.
    """

    __tablename__ = "kb_work_approvals"
    __table_args__ = (
        CheckConstraint("decision in ('approved','rejected')", name="kb_work_approvals_decision"),
        UniqueConstraint("clinician_id", "work_id", name="kb_work_approvals_clinician_work"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    clinician_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_works.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(16))  # approved | rejected
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class AssistantThread(Base):
    """Сессия клинического ассистента врача (FR-21): контекст диалога живёт
    между запросами. Область пациентов — assistant_thread_patients (мультипациент).
    """

    __tablename__ = "assistant_threads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    clinician_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(EncryptedText, default="Новый диалог")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()"), index=True
    )


class AssistantThreadPatient(Base):
    """Область пациентов треда. Только roster-пациенты врача (гейт в сервисе)."""

    __tablename__ = "assistant_thread_patients"

    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistant_threads.id", ondelete="CASCADE"), primary_key=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)


class AssistantMessage(Base):
    """Сообщение треда: вопрос врача или ответ ассистента. Текст — спецкатегория
    ПДн (может упоминать пациента/симптомы) → EncryptedText. citations —
    публичные источники KB (не ПДн), topic — раздел БЗ для роутинга.
    """

    __tablename__ = "assistant_messages"
    __table_args__ = (
        CheckConstraint("role in ('CLINICIAN','ASSISTANT')", name="assistant_messages_role"),
        Index("ix_assistant_messages_thread_created", "thread_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistant_threads.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # CLINICIAN | ASSISTANT
    content: Mapped[str] = mapped_column(EncryptedText)
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    topic: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), index=True)
class EmiasLink(Base):
    """Связка локального пациента с пациентом ЕМИАС (FR-25).

    Один пациент — одна связка (patient_id unique). СНИЛС НЕ хранится в открытом
    виде: только sha256(СНИЛС + перец) — хватает для идемпотентного поиска,
    недостаточно для восстановления идентификатора (152-ФЗ ст.10).
    """

    __tablename__ = "emias_links"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # unique уже создаёт индекс: отдельный index=True давал дублирующий индекс.
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), unique=True)
    # один пациент ЕМИАС — одна карта локально (иначе двойная связка и дубли визитов).
    emias_patient_id: Mapped[str] = mapped_column(String(64), unique=True)
    snils_hash: Mapped[str] = mapped_column(String(64), index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class PatientMedication(Base):
    """Назначение препарата (FR-23, FR-25).

    Два источника: 'emias' — импорт из ЕМИАС (idempotent upsert по emias_id,
    clinician_id NULL, строка read-only для врача) и 'clinician' — назначение
    врача в карточке (clinician_id, правится/останавливается/удаляется автором).
    Название, дозировка и заметка — спецкатегория ПДн (косвенно указывают
    диагноз) → EncryptedText (pgcrypto, CC_DATA_KEY).
    """

    __tablename__ = "patient_medications"
    __table_args__ = (
        CheckConstraint("status in ('active','completed','stopped','unknown')", name="patient_medications_status"),
        CheckConstraint("source in ('emias','clinician')", name="patient_medications_source"),
        CheckConstraint(
            "ended_on is null or started_on is null or ended_on >= started_on",
            name="patient_medications_dates",
        ),
        # FR-27: режим приёма — знаменатель приверженности. NULL — расписание
        # неизвестно (все строки импорта ЕМИАС), процент не считается.
        CheckConstraint(
            "times_per_day is null or times_per_day between 1 and 6",
            name="patient_medications_times_per_day",
        ),
        # emias_id уникален только среди импортированных строк: у локальных
        # назначений его нет, NULL не должен блокировать вставку.
        Index(
            "uq_patient_medications_emias_id",
            "emias_id",
            unique=True,
            postgresql_where=text("emias_id is not null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    clinician_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    title: Mapped[str] = mapped_column(EncryptedText())
    dosage: Mapped[str | None] = mapped_column(EncryptedText(), nullable=True)
    started_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    ended_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    notes: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="clinician", server_default=text("'clinician'"))
    emias_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    # FR-27: сколько раз в день принимать. NULL — режим не задан (ЕМИАС).
    times_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)

class EmiasSyncRun(Base):
    """Прогон синхронизации с ЕМИАС (по образцу KBRun, docs/15 §4).

    stats — счётчики fetched/created/updated/skipped/errors и список ошибок
    по сущностям; тела ответов ЕМИАС и ПДн сюда не попадают.
    """

    __tablename__ = "emias_sync_runs"
    __table_args__ = (
        CheckConstraint("status in ('running','ok','partial','failed')", name="emias_sync_runs_status"),
        CheckConstraint("mode in ('patients','one')", name="emias_sync_runs_mode"),
        Index("ix_emias_sync_runs_started_at", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    trigger: Mapped[str] = mapped_column(String(16))  # manual | cli
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="running")
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatInsight(Base):
    """FR-31: факт из переписки врача с пациентом — черновик до решения врача.

    Источник — явные высказывания пациента (LLM извлекает только их, docs/17).
    text — спецкатегория ПДн, поэтому EncryptedText (pgcrypto, F1). Строка живёт
    только пока пациент дал согласие: отзыв уничтожает её (consent router, F2).
    dedupe_hash = sha256(patient_id|kind|normalized text) — повторный анализ не
    плодит дубликаты; source_message_ids — id сообщений-оснований (без текста).
    """

    __tablename__ = "chat_insights"
    __table_args__ = (
        CheckConstraint(
            "kind in ('symptom','medication','adherence','sleep','substance','risk','other')",
            name="chat_insights_kind",
        ),
        CheckConstraint(
            "status in ('pending','accepted','rejected')", name="chat_insights_status"
        ),
        UniqueConstraint("patient_id", "dedupe_hash", name="chat_insights_patient_dedupe"),
        Index("ix_chat_insights_patient_status", "patient_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(EncryptedText())
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_message_ids: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    dedupe_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class PatientAnamnesis(Base):
    """FR-37: анамнез пациента — вводные врача и сформированный текст.

    Одна строка на пациента (patient_id UNIQUE). Оба поля — спецкатегория ПДн
    (косвенно указывают диагноз), поэтому EncryptedText (pgcrypto).
    `brief` — краткие вводные, которые врач правит и отправляет в LLM;
    `text` — структурированный анамнез (черновик модели после подтверждения
    врачом). Аудит пишет только ids и категории масок, тексты — никогда.
    """

    __tablename__ = "patient_anamnesis"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), unique=True)
    brief: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    updated_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    # Поле `text` объявлено последним: имя класса-скоупа иначе затеняет
    # sqlalchemy.text в server_default выше.
    text: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)


class Appointment(Base):
    """FR-38: запись в календаре врача (docs/18 §3.1) — план приёма, не факт.

    Факт приёма — `clinical_sessions`; запись связывается с ним через
    `clinical_sessions.appointment_id`. Пересечения интервалов у одного врача
    запрещены на уровне сервиса (409 SLOT_CONFLICT) под `SELECT … FOR UPDATE`
    по врачу — exclusion constraint/btree_gist не вводим (docs/18 §3.1).
    `location` — кабинет или ссылка, не ПДн.
    """

    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("duration_min between 10 and 480", name="appointments_duration"),
        CheckConstraint(
            "channel in ('in_person','online','phone')", name="appointments_channel"
        ),
        CheckConstraint(
            "status in ('planned','confirmed','completed','cancelled','no_show')",
            name="appointments_status",
        ),
        Index("appointments_clinician_starts", "clinician_id", "starts_at"),
        Index("appointments_patient_starts", "patient_id", "starts_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    clinician_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_min: Mapped[int] = mapped_column(
        Integer, server_default=text("50")
    )  # 10..480, дефолт приёма врача — 50 мин
    channel: Mapped[str] = mapped_column(
        String(16), server_default=text("'in_person'")
    )  # in_person | online | phone
    status: Mapped[str] = mapped_column(
        String(16), server_default=text("'planned'")
    )  # planned | confirmed | completed | cancelled | no_show
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class ClinicalSession(Base):
    """FR-38/39: фактическая клиническая сессия (docs/18 §3.2).

    `appointment_id IS NULL` — приём «без записи в календаре». `audio_consent` —
    отдельное согласие на запись (docs/18 §1.3), не покрытое согласием 152-ФЗ:
    без него загрузка аудио запрещена (422 AUDIO_CONSENT_REQUIRED, PR P3).
    `ai_status` — состояние ИИ-блока (PR P3), по умолчанию `off`.
    """

    __tablename__ = "clinical_sessions"
    __table_args__ = (
        CheckConstraint(
            "channel in ('in_person','online','phone')", name="clinical_sessions_channel"
        ),
        CheckConstraint("status in ('active','finished')", name="clinical_sessions_status"),
        CheckConstraint(
            "ai_status in ('off','recording','transcribing','transcribed','failed')",
            name="clinical_sessions_ai_status",
        ),
        CheckConstraint(
            "ended_at is null or ended_at >= started_at", name="clinical_sessions_interval"
        ),
        # Одна сессия на запись календаря: partial unique (offline-сессии не мешают).
        Index(
            "clinical_sessions_appointment",
            "appointment_id",
            unique=True,
            postgresql_where=text("appointment_id is not null"),
        ),
        Index("clinical_sessions_clinician_started", "clinician_id", "started_at"),
        Index("clinical_sessions_patient_started", "patient_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id"), nullable=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    clinician_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    channel: Mapped[str] = mapped_column(String(16), server_default=text("'in_person'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'active'"))
    audio_consent: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )  # отдельное согласие на запись (docs/18 §1.3)
    ai_status: Mapped[str] = mapped_column(String(16), server_default=text("'off'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class SessionProtocol(Base):
    """FR-39: протокол приёма — версии записи врача (docs/18 §3.4, §5).

    `sections` (канонический JSON §5.1) — свободный текст, поэтому EncryptedText:
    в БД только шифротекст (pgcrypto, ключ CC_DATA_KEY). Хэш `content_sha256`
    считается по каноническому JSON и заполняется при approve — эти же байты
    подписываются УКЭП в PR P4. Активна максимум одна версия из
    `draft|approved|signed|submitted` (инвариант сервиса, docs/18 §3.4).
    """

    __tablename__ = "session_protocols"
    __table_args__ = (
        CheckConstraint(
            "status in ('draft','approved','signed','submitted','superseded','rejected')",
            name="session_protocols_status",
        ),
        CheckConstraint("source in ('manual','llm')", name="session_protocols_source"),
        UniqueConstraint("session_id", "version", name="session_protocols_session_version"),
        Index("session_protocols_session_status", "session_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinical_sessions.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'draft'"))
    source: Mapped[str] = mapped_column(String(16), server_default=text("'manual'"))
    # Канонический JSON разделов §5.1; свободный текст → шифруется (F1).
    sections_cipher: Mapped[str] = mapped_column(EncryptedText())
    content_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)  # hex sha256
    diagnosis_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # [{"section": "...", "reason": "..."}] — заполняет ИИ-блок (PR P3).
    needs_review: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class DocumentArtifact(Base):
    """FR-38…41: шифрованный файл сессии (docs/18 §3.6, §4.6).

    Аудио, расшифровки, PDF, подпись — спецкатегория ПДн: на диске только
    AES-256-GCM (сервис `app.services.artifacts`), в БД — метаданные без текста.
    `sha256` — хэш **шифротекста** (сверка целостности файла), `path` —
    относительный путь под CC_ARTIFACTS_DIR, без ПДн в имени.
    """

    __tablename__ = "document_artifacts"
    __table_args__ = (
        CheckConstraint(
            "kind in ('audio','transcript','protocol_pdf','protocol_cda','signature')",
            name="document_artifacts_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(24))
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clinical_sessions.id"), nullable=True, index=True
    )
    protocol_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("session_protocols.id"), nullable=True
    )
    path: Mapped[str] = mapped_column(String(400))
    sha256: Mapped[str] = mapped_column(CHAR(64))  # §3.6: char(64), хэш шифротекста
    size_bytes: Mapped[int] = mapped_column(Integer)
    mime: Mapped[str] = mapped_column(String(128))
    encrypted: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class SessionTranscript(Base):
    """FR-39 (PR P3): метаданные расшифровки сессии (docs/18 §3.5).

    Текст сегментов живёт только в шифрованном артефакте (`segments_artifact_id`):
    в jsonb — категории масок, в аудите — только счётчики. Одна расшифровка на
    сессию (UNIQUE session_id).
    """

    __tablename__ = "session_transcripts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clinical_sessions.id"), unique=True
    )
    language: Mapped[str] = mapped_column(String(2), server_default=text("'ru'"))
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    segments_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_artifacts.id"), nullable=True
    )
    mask_categories: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    depersonalized: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ProtocolSignature(Base):
    """FR-40: подпись протокола приёма (docs/18 §3.7, §7).

    Две строки на протокол: `clinician` (врач, подписавший документ) и `mo`
    (уполномоченное лицо МО — п. 11 приказа Минздрава 209н). Закрытые ключи в
    БД не попадают никогда: только сам CMS-контейнер (шифрованный артефакт
    `kind='signature'`) и публичные данные сертификата.

    `cert_der` хранится рядом с подписью, чтобы проверка не зависела от
    доступности демо-УЦ в момент verify и не подтягивала сертификат по сети.
    """

    __tablename__ = "protocol_signatures"
    __table_args__ = (
        CheckConstraint("role in ('clinician','mo')", name="protocol_signatures_role"),
        UniqueConstraint("protocol_id", "role", name="protocol_signatures_protocol_role"),
        Index("protocol_signatures_session", "session_id", "role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    protocol_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session_protocols.id"))
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinical_sessions.id"))
    role: Mapped[str] = mapped_column(String(16))
    # Врач существует как пользователь; у уполномоченного лица МО строки users нет.
    signer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    fio: Mapped[str] = mapped_column(String(200))
    position: Mapped[str] = mapped_column(String(200))
    organization: Mapped[str] = mapped_column(String(200))
    snils: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cert_serial: Mapped[str] = mapped_column(String(64))
    cert_issuer: Mapped[str] = mapped_column(String(400))
    cert_not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cert_not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cert_thumbprint: Mapped[str] = mapped_column(CHAR(64))
    cert_der: Mapped[bytes] = mapped_column(LargeBinary)
    # Деташированная CMS-подпись — только в шифрованном артефакте (§3.6).
    cms_artifact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_artifacts.id"))
    digest_algorithm: Mapped[str] = mapped_column(String(32), server_default=text("'GOST3411-2012-256'"))
    signature_algorithm: Mapped[str] = mapped_column(
        String(32), server_default=text("'GOST3410-2012-256'")
    )
    # Стрибог-256 подписанных байт СЭМД (hex) — сравнение при verify.
    content_sha256: Mapped[str] = mapped_column(CHAR(64))
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
