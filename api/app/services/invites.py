"""FR-49: приглашение пациента врачом (ссылка вместо ручного админа).

Врач заполняет карту (ФИО, телефон, дата рождения, опционально e-mail) и
получает одноразовую ссылку `/invite/<token>`. Пациент открывает её, задаёт
пароль (и e-mail, если врач его не знал) — только тогда создаётся `users`-строка
и запись `clinician_roster`. Токен живёт 7 дней, хранится хешем и показывается
ровно один раз: повторно ссылку выдают перевыпуском.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import Problem
from app.models import ClinicianRoster, PatientInvite, User
from app.services import audit
from app.services.contacts import MIN_BIRTH_DATE, normalize_phone

TOKEN_BYTES = 32  # 256 бит: ссылку не подобрать перебором
TOKEN_TTL = timedelta(days=7)
# Полка на «висящие» приглашения одного врача: без неё список превращается в
# свалку, а каждая строка — это потенциальный вход в карту.
MAX_PENDING = 200
INVITE_PATH_PREFIX = "/invite/"


def _token_hash(token: str) -> str:
    """sha256(token + CC_DATA_KEY): в БД токена нет, как и кодов из писем."""
    return hashlib.sha256(f"{token}:{settings.data_key}".encode()).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def _clean_email(raw: str | None) -> str | None:
    if raw is None:
        return None
    email = raw.strip().lower()
    if not email:
        return None
    # Та же планка, что у регистрации (`EmailStr` тут не используется):
    # ровно одна «собака», непустые части, без пробелов.
    if email.count("@") != 1 or any(c.isspace() for c in email):
        raise Problem(422, "EMAIL_INVALID", "Некорректный e-mail")
    local, domain = email.split("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise Problem(422, "EMAIL_INVALID", "Некорректный e-mail")
    return email


def _check_birth_date(value: date | None) -> date | None:
    if value is None:
        return None
    if value < MIN_BIRTH_DATE or value > date.today():
        raise Problem(422, "BIRTH_DATE_INVALID", "Дата рождения вне допустимого диапазона")
    return value


async def _pending_count(db: AsyncSession, clinician_id: uuid.UUID) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(PatientInvite)
            .where(
                PatientInvite.clinician_id == clinician_id,
                PatientInvite.accepted_at.is_(None),
            )
        )
        or 0
    )


async def create(
    db: AsyncSession,
    clinician_id: uuid.UUID,
    *,
    fio: str | None,
    phone: str | None,
    birth_date: date | None,
    email: str | None,
    ip: str | None = None,
) -> tuple[PatientInvite, str]:
    """Создать приглашение; возвращает (строку, токен). Токен не хранится."""
    # Просроченные непринятые строки удаляем: они не видны в списке (list_pending
    # фильтрует по expires_at), поэтому иначе они навсегда занимали бы лимит и
    # врач упирался бы в «отзовите лишние», не видя, что отзывать.
    await db.execute(
        delete(PatientInvite).where(
            PatientInvite.accepted_at.is_(None),
            PatientInvite.expires_at <= datetime.now(timezone.utc),
        )
    )
    if await _pending_count(db, clinician_id) >= MAX_PENDING:
        raise Problem(
            409, "INVITE_LIMIT", "Слишком много непринятых приглашений — отзовите лишние"
        )
    token = _new_token()
    invite = PatientInvite(
        clinician_id=clinician_id,
        token_hash=_token_hash(token),
        fio=(fio or "").strip() or None,
        phone=normalize_phone(phone),
        birth_date=_check_birth_date(birth_date),
        email=_clean_email(email),
        expires_at=datetime.now(timezone.utc) + TOKEN_TTL,
    )
    db.add(invite)
    await db.flush()
    await audit.record(db, clinician_id, "INVITE_CREATE", "patient_invite", str(invite.id), ip)
    await db.commit()
    return invite, token


async def get_owned(
    db: AsyncSession,
    clinician_id: uuid.UUID,
    invite_id: uuid.UUID | str,
    *,
    lock: bool = False,
) -> PatientInvite:
    """Приглашение владельца-врача; чужое и несуществующее — одинаковый 404.

    `lock=True` — `FOR UPDATE`: перевыпуск и отзыв не должны гоняться с
    активацией той же строки.
    """
    try:
        invite_uuid = uuid.UUID(str(invite_id))
    except ValueError:
        raise Problem(404, "INVITE_NOT_FOUND", "Приглашение не найдено") from None
    stmt = select(PatientInvite).where(PatientInvite.id == invite_uuid)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    invite = await db.scalar(stmt)
    if invite is None or invite.clinician_id != clinician_id:
        raise Problem(404, "INVITE_NOT_FOUND", "Приглашение не найдено")
    return invite


async def reissue(
    db: AsyncSession, clinician_id: uuid.UUID, invite_id: uuid.UUID | str, ip: str | None = None
) -> tuple[PatientInvite, str]:
    """Новый токен вместо старого: ссылку нельзя показать повторно из БД."""
    invite = await get_owned(db, clinician_id, invite_id, lock=True)
    if invite.accepted_at is not None:
        raise Problem(409, "INVITE_ACCEPTED", "Приглашение уже принято")

    token = _new_token()
    invite.token_hash = _token_hash(token)
    invite.expires_at = datetime.now(timezone.utc) + TOKEN_TTL
    await audit.record(
        db, clinician_id, "INVITE_REISSUE", "patient_invite", str(invite.id), ip
    )
    await db.commit()
    # Старый хеш затёрт: ссылка из прежнего сообщения больше не работает —
    # единственный способ «погасить» забытый токен.
    return invite, token


async def revoke(
    db: AsyncSession, clinician_id: uuid.UUID, invite_id: uuid.UUID | str, ip: str | None = None
) -> None:
    invite = await get_owned(db, clinician_id, invite_id, lock=True)
    if invite.accepted_at is not None:
        raise Problem(409, "INVITE_ACCEPTED", "Приглашение уже принято")
    await db.delete(invite)
    await audit.record(db, clinician_id, "INVITE_REVOKE", "patient_invite", str(invite.id), ip)
    await db.commit()


async def list_pending(db: AsyncSession, clinician_id: uuid.UUID) -> list[PatientInvite]:
    """Живые непринятые приглашения врача, новые сверху."""
    rows = await db.scalars(
        select(PatientInvite)
        .where(
            PatientInvite.clinician_id == clinician_id,
            PatientInvite.accepted_at.is_(None),
            PatientInvite.expires_at > datetime.now(timezone.utc),
        )
        .order_by(PatientInvite.created_at.desc())
    )
    return list(rows)


async def load(db: AsyncSession, token: str, *, lock: bool = False) -> PatientInvite:
    """Публичная выборка по токену: неизвестный — 404, принятый/просроченный — 410.

    `lock=True` (активация) берёт строку под `FOR UPDATE`: два одновременных
    принятия одной ссылки иначе создали бы два аккаунта на одну карту.
    """
    if not token or len(token) > 128:
        raise Problem(404, "INVITE_NOT_FOUND", "Приглашение не найдено")
    stmt = select(PatientInvite).where(PatientInvite.token_hash == _token_hash(token))
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    invite = await db.scalar(stmt)
    if invite is None:
        raise Problem(404, "INVITE_NOT_FOUND", "Приглашение не найдено")
    if invite.accepted_at is not None:
        raise Problem(410, "INVITE_USED", "Приглашение уже использовано")
    if invite.expires_at <= datetime.now(timezone.utc):
        raise Problem(410, "INVITE_EXPIRED", "Срок приглашения истёк")
    return invite


async def accept(
    db: AsyncSession,
    invite: PatientInvite,
    *,
    email: str | None,
    password_hash: str,
    ip: str | None = None,
) -> User:
    """Создать аккаунт пациента из карты врача и закрепить его за врачом."""
    resolved = invite.email or _clean_email(email)
    if not resolved:
        raise Problem(422, "EMAIL_REQUIRED", "Укажите e-mail — он будет логином")
    if await db.scalar(select(User).where(User.email == resolved)) is not None:
        raise Problem(409, "EMAIL_TAKEN", "Этот адрес уже занят: войдите или восстановите пароль")
    user = User(
        email=resolved,
        fio=invite.fio,
        phone=invite.phone,
        birth_date=invite.birth_date,
        password_hash=password_hash,
        role="PATIENT",
    )
    # Savepoint: между проверкой и вставкой адрес мог занять параллельный
    # запрос (`ix_users_email`), и без перехвата это был бы 500 вместо 409.
    try:
        async with db.begin_nested():
            db.add(user)
            await db.flush()
    except IntegrityError:
        raise Problem(
            409, "EMAIL_TAKEN", "Этот адрес уже занят: войдите или восстановите пароль"
        ) from None
    db.add(ClinicianRoster(clinician_id=invite.clinician_id, patient_id=user.id))
    invite.accepted_at = datetime.now(timezone.utc)
    invite.accepted_patient_id = user.id
    # Карта перенесена в аккаунт — в приглашении ПДн больше не нужны. Так
    # отзыв согласия пациента не оставляет ФИО/телефон/дату рождения в
    # строке, которую `_destroy_patient_pdn` не трогает.
    invite.fio = None
    invite.phone = None
    invite.birth_date = None
    invite.email = None
    await audit.record(db, user.id, "INVITE_ACCEPT", "patient_invite", str(invite.id), ip)
    await db.commit()
    return user
