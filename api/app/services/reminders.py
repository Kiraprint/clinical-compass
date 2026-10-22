"""FR-24: личные напоминания врача по пациентам.

Врач ставит напоминание, привязанное к пациенту из своего roster: позвонить /
написать / назначить приём / свободный текст + срок (due_at). Сервис отдаёт
списки с состоянием срока (overdue/today/soon/later) и закрывает напоминания
(done/cancelled). Все операции ограничены clinician_id владельца — чужое
напоминание невидимо (404) и неизменяемо.

Доставка — без внешнего шедулера: `python -m app.reminders_worker` периодически
зовёт `deliver_due`. На каждое просроченное-к-доставке напоминание создаётся
ровно один in-app Notification (дедуп: unique notifications.reminder_id +
notified_at) и best-effort push подпискам врача. В push/уведомлении только ФИО
пациента и вид напоминания — свободный текст note (может содержать ФИО/состояние)
остаётся в карточке и не уходит в канал доставки.

note — спецкатегория ПДн (свободный текст), поэтому EncryptedText (F1).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.engine import Connection

from app.errors import Problem
from app.models import ClinicianReminder, Notification, User
from app.services import push

from .clinician import require_roster

log = logging.getLogger(__name__)

# Ключ advisory-лока воркера: параллельные инстансы не дублируют доставку.
DELIVERY_LOCK_KEY = "clinician-reminders"
# Предел выборки за тик: ограничивает латентность и объём push-рассылки.
DELIVERY_BATCH = 200

KIND_LABELS = {
    "call": "Позвонить",
    "write": "Написать",
    "appointment": "Записать на приём",
    "custom": "Другое",
}

STATUSES = ("open", "done", "cancelled")

# Рабочий часовой пояс клиники: граница «сегодня» — календарный день МСК,
# а не UTC (иначе напоминания на 01:00 МСК попадают в «вчера»).
CLINIC_TZ = ZoneInfo("Europe/Moscow")

def _now() -> datetime:
    """Текущий момент (тесты подменяют функцию, чтобы заморозить время)."""
    return datetime.now(timezone.utc)

def _as_aware(value: datetime) -> datetime:
    """Naive datetime из JSON трактуем как UTC; aware приводим к UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None

def _day_window(now: datetime) -> tuple[datetime, datetime]:
    """Полуночные границы «сегодня» в часовом поясе клиники (в UTC)."""
    local = now.astimezone(CLINIC_TZ)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)

def _due_state(due_at: datetime, now: datetime) -> str:
    """overdue | today | soon (≤ 7 дней) | later — для группировки в UI."""
    if due_at < now:
        return "overdue"
    start, end = _day_window(now)
    if start <= due_at < end:
        return "today"
    if due_at <= now + timedelta(days=7):
        return "soon"
    return "later"

def _patient_name(db: Session, patient_id: uuid.UUID) -> str:
    patient = db.get(User, patient_id)
    if patient is None:
        return "пациент"
    return patient.fio or "пациент"

def _serialize(row: ClinicianReminder, patient_fio: str, now: datetime) -> dict:
    return {
        "id": str(row.id),
        "patient_id": str(row.patient_id),
        "patient_fio": patient_fio,
        "kind": row.kind,
        "kind_label": KIND_LABELS.get(row.kind, row.kind),
        "note": row.note,
        "due_at": _iso(row.due_at),
        "status": row.status,
        "due_state": _due_state(row.due_at, now) if row.status == "open" else None,
        "overdue_minutes": (
            max(0, int((now - row.due_at).total_seconds() // 60))
            if row.status == "open" and row.due_at < now
            else 0
        ),
        "done_at": _iso(row.done_at),
        "notified_at": _iso(row.notified_at),
        "created_at": _iso(row.created_at),
    }

def _get_owned(db: Session, clinician_id: uuid.UUID, reminder_id: uuid.UUID) -> ClinicianReminder:
    """Напоминание владельца; чужое → 404 (не подтверждаем существование — IDOR)."""
    row = db.get(ClinicianReminder, reminder_id)
    if row is None or row.clinician_id != clinician_id:
        raise Problem(404, "NOT_FOUND", "Напоминание не найдено")
    return row

def _check_kind(kind: str) -> str:
    if kind not in KIND_LABELS:
        raise Problem(422, "VALIDATION", "Неизвестный вид напоминания")
    return kind

def create(
    db: Session,
    clinician_id: uuid.UUID,
    patient_id: uuid.UUID,
    kind: str,
    note: str | None,
    due_at: datetime,
) -> dict:
    require_roster(db, clinician_id, patient_id)
    row = ClinicianReminder(
        clinician_id=clinician_id,
        patient_id=patient_id,
        kind=_check_kind(kind),
        note=(note or None),
        due_at=_as_aware(due_at),
        status="open",
    )
    db.add(row)
    db.commit()
    return _serialize(row, _patient_name(db, patient_id), _now())

def list_reminders(
    db: Session,
    clinician_id: uuid.UUID,
    status: str = "open",
    due: str | None = None,
    patient_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> dict:
    """Список напоминаний врача.

    due=overdue — срок уже прошёл (open); today — календарный день МСК;
    week — ближайшие 7 дней, просроченные входят (врач видит хвост).
    """
    now = now or _now()
    q = select(ClinicianReminder).where(ClinicianReminder.clinician_id == clinician_id)
    if status != "all":
        if status not in STATUSES:
            raise Problem(422, "VALIDATION", "Неизвестный статус напоминания")
        q = q.where(ClinicianReminder.status == status)
    if patient_id is not None:
        q = q.where(ClinicianReminder.patient_id == patient_id)
    if due is not None:
        if due == "overdue":
            q = q.where(ClinicianReminder.due_at < now)
        elif due == "today":
            start, end = _day_window(now)
            q = q.where(ClinicianReminder.due_at >= start, ClinicianReminder.due_at < end)
        elif due == "week":
            q = q.where(ClinicianReminder.due_at <= now + timedelta(days=7))
        else:
            raise Problem(422, "VALIDATION", "due: ожидается overdue|today|week")
    rows = db.scalars(q.order_by(ClinicianReminder.due_at, ClinicianReminder.id)).all()

    names: dict[uuid.UUID, str] = {}
    items = []
    for row in rows:
        if row.patient_id not in names:
            names[row.patient_id] = _patient_name(db, row.patient_id)
        items.append(_serialize(row, names[row.patient_id], now))

    overdue_q = select(func.count(ClinicianReminder.id)).where(
        ClinicianReminder.clinician_id == clinician_id,
        ClinicianReminder.status == "open",
        ClinicianReminder.due_at < now,
    )
    if patient_id is not None:
        overdue_q = overdue_q.where(ClinicianReminder.patient_id == patient_id)
    overdue = int(db.scalar(overdue_q) or 0)
    return {"items": items, "overdue": overdue}

def update(
    db: Session,
    clinician_id: uuid.UUID,
    reminder_id: uuid.UUID,
    *,
    note: str | None = None,
    note_set: bool = False,
    due_at: datetime | None = None,
    kind: str | None = None,
) -> dict:
    """Правка напоминания: note/due_at/kind. Закрытое (done/cancelled) не меняется."""
    row = _get_owned(db, clinician_id, reminder_id)
    if row.status != "open":
        raise Problem(409, "REMINDER_CLOSED", "Напоминание уже закрыто")
    if note_set:
        row.note = note or None
    if due_at is not None:
        # Перенос срока не сбрасывает notified_at: уведомление о напоминании уже
        # доставлено, дедуп по unique notifications.reminder_id не даст второго,
        # а новый срок виден в панели (повторный push — осознанно вне MVP).
        row.due_at = _as_aware(due_at)
    if kind is not None:
        row.kind = _check_kind(kind)
    db.commit()
    return _serialize(row, _patient_name(db, row.patient_id), _now())

def complete(db: Session, clinician_id: uuid.UUID, reminder_id: uuid.UUID) -> dict:
    row = _get_owned(db, clinician_id, reminder_id)
    if row.status != "open":
        raise Problem(409, "REMINDER_CLOSED", "Напоминание уже закрыто")
    row.status = "done"
    row.done_at = _now()
    db.commit()
    return _serialize(row, _patient_name(db, row.patient_id), _now())

def cancel(db: Session, clinician_id: uuid.UUID, reminder_id: uuid.UUID) -> dict:
    row = _get_owned(db, clinician_id, reminder_id)
    if row.status != "open":
        raise Problem(409, "REMINDER_CLOSED", "Напоминание уже закрыто")
    row.status = "cancelled"
    row.done_at = _now()
    db.commit()
    return _serialize(row, _patient_name(db, row.patient_id), _now())

def delete_for_patient(db: Session, patient_id: uuid.UUID) -> int:
    """Уничтожение напоминаний пациента (отзыв согласия, 152-ФЗ ст.9 ч.2-3).

    Вызывается из routers/consent._destroy_patient_pdn ПОСЛЕ удаления
    notifications (у тех FK reminder_id), коммитит внешний код.
    """
    from sqlalchemy import delete as sa_delete

    result = db.execute(sa_delete(ClinicianReminder).where(ClinicianReminder.patient_id == patient_id))
    return int(result.rowcount or 0)

def deliver_due(db: Session, now: datetime | None = None) -> int:
    """Тик доставки: open + due_at <= now + notified_at is null → Notification + push.

    Идемпотентно: notified_at ставится в той же транзакции, что и Notification;
    unique notifications.reminder_id — второй барьер от дублей. Advisory lock
    (pg_try_advisory_lock) не даёт двум воркерам обработать один тик параллельно.
    Возвращает число доставленных напоминаний.
    """
    now = now or _now()
    bind = db.get_bind()
    engine = bind.engine if isinstance(bind, Connection) else bind
    # Лок берём на отдельном соединении: после ошибки транзакция сессии может
    # быть aborted, и pg_advisory_unlock на ней же не выполнится — лок утечёт
    # на пул-соединение и заблокирует все следующие тики этого воркера.
    lock_conn = engine.connect()
    pushes: list[tuple[uuid.UUID, str, str]] = []
    try:
        locked = bool(
            lock_conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": DELIVERY_LOCK_KEY}
            ).scalar()
        )
        if not locked:
            log.info("reminders: другой воркер уже держит лок, пропуск тика")
            return 0
        try:
            rows = db.scalars(
                select(ClinicianReminder)
                .where(
                    ClinicianReminder.status == "open",
                    ClinicianReminder.due_at <= now,
                    ClinicianReminder.notified_at.is_(None),
                )
                .order_by(ClinicianReminder.due_at)
                .limit(DELIVERY_BATCH)
                .with_for_update(skip_locked=True)
            ).all()
            if not rows:
                db.rollback()
                return 0

            names: dict[uuid.UUID, str] = {}
            for row in rows:
                if row.patient_id not in names:
                    names[row.patient_id] = _patient_name(db, row.patient_id)
                fio = names[row.patient_id]
                label = KIND_LABELS.get(row.kind, row.kind)
                title = "Напоминание врача"
                body = f"{fio}: {label.lower()}"
                exists = db.scalar(
                    select(Notification.id).where(Notification.reminder_id == row.id).limit(1)
                )
                if exists is None:
                    db.add(
                        Notification(
                            user_id=row.clinician_id,
                            kind="REMINDER",
                            title=title,
                            body=body,
                            patient_id=row.patient_id,
                            reminder_id=row.id,
                        )
                    )
                row.notified_at = now
                pushes.append((row.clinician_id, title, body))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            lock_conn.execute(
                text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": DELIVERY_LOCK_KEY}
            )
            lock_conn.commit()
    finally:
        lock_conn.close()

    # Push — best-effort и вне критической секции лока: сбой доставки не откатывает БД.
    for clinician_id, title, body in pushes:
        push.push_to_clinicians(db, [clinician_id], title, body)
    return len(pushes)
