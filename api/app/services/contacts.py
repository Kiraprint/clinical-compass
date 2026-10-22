"""FR-22: контакты пациента (телефон, дата рождения).

Валидация и нормализация ввода, маскирование для витрин (список/карта),
roster/consent-гейты и аудит доступа. Телефон хранится зашифрованным
(EncryptedText, F1); в ответах списка/карты — только маска (+7•••123-45-67),
полное значение отдаётся лишь выделенным endpoint'ом контактов (аудит READ_*).
"""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import Problem
from app.models import Consent, User

from .audit import record as audit_record

# Разделители, которые люди реально печатают в номере: пробел, дефис, скобки, точка.
_PHONE_SEPARATORS = re.compile(r"[\s\-\u2010-\u2015()\.]")

MIN_BIRTH_DATE = date(1900, 1, 1)


def normalize_phone(raw: object) -> str | None:
    """`+7XXXXXXXXXX` из ввода `+7…`/`8…`/`7…`/10 цифр; иначе 422 PHONE_INVALID.

    Пустая строка/None → None (очистка значения). Номера других стран и
    неоднозначные длины отклоняются: сервис работает только с РФ-нумерацией.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise Problem(422, "PHONE_INVALID", "Телефон должен быть строкой")
    compact = _PHONE_SEPARATORS.sub("", raw)
    if compact == "":
        return None
    # Только ASCII-цифры: str.isdigit() принимает арабо-индийские и надстрочные
    # цифры, из которых получается недозваниваемый «номер».
    explicit_plus = compact.startswith("+")
    digits = compact[1:] if explicit_plus else compact
    if not (digits.isascii() and digits.isdigit()):
        raise Problem(422, "PHONE_INVALID", "Телефон содержит недопустимые символы")
    if explicit_plus:
        # Явный `+` — только полный международный РФ-формат +7XXXXXXXXXX;
        # иначе усечённый ввод вида +7926123456 молча стал бы другим номером.
        if len(digits) == 11 and digits[0] == "7":
            return "+" + digits
        raise Problem(422, "PHONE_INVALID", "Ожидается российский номер: +7XXXXXXXXXX, 8XXXXXXXXXX или 10 цифр")
    if len(digits) == 10:  # 9XXXXXXXXX без кода страны
        return "+7" + digits
    if len(digits) == 11 and digits[0] in ("7", "8"):  # 7XXXXXXXXXX / 8XXXXXXXXXX
        return "+7" + digits[1:]
    raise Problem(422, "PHONE_INVALID", "Ожидается российский номер: +7XXXXXXXXXX, 8XXXXXXXXXX или 10 цифр")


def mask_phone(phone: str | None) -> str | None:
    """Маска для витрин: `+7•••123-45-67` (последние 7 цифр)."""
    if not phone:
        return None
    digits = phone[-10:]
    return f"+7•••{digits[3:6]}-{digits[6:8]}-{digits[8:10]}"


def parse_birth_date(raw: object) -> date | None:
    """Дата рождения из ISO `YYYY-MM-DD` в пределах 1900..сегодня.

    Будущая/слишком старая/невалидная дата → 422 BIRTH_DATE_INVALID.
    Возраст <18 допустим к хранению — клинические ограничения не выдумываем,
    ответ помечает adult=false.
    """
    if raw is None:
        return None
    if isinstance(raw, date):
        value = raw
    elif isinstance(raw, str):
        try:
            value = date.fromisoformat(raw.strip())
        except ValueError:
            raise Problem(422, "BIRTH_DATE_INVALID", "Ожидается дата в формате YYYY-MM-DD") from None
    else:
        raise Problem(422, "BIRTH_DATE_INVALID", "Ожидается дата в формате YYYY-MM-DD")
    if value < MIN_BIRTH_DATE:
        raise Problem(422, "BIRTH_DATE_INVALID", "Дата рождения раньше 1900 года")
    if value > date.today():
        raise Problem(422, "BIRTH_DATE_INVALID", "Дата рождения в будущем")
    return value


def age_of(birth_date: date | None) -> int | None:
    if birth_date is None:
        return None
    today = date.today()
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))


def contacts_payload(user: User, *, masked: bool = False) -> dict:
    """Единый формат контактов: phone, birth_date, age, adult."""
    age = age_of(user.birth_date)
    return {
        "phone": mask_phone(user.phone) if masked else user.phone,
        "birth_date": user.birth_date.isoformat() if user.birth_date else None,
        "age": age,
        "adult": None if age is None else age >= 18,
    }


def apply_contacts(user: User, changes: dict) -> None:
    """Применить только переданные поля (PATCH-семантика) после валидации.

    Значение None очищает поле; отсутствие ключа — не трогаем.
    """
    if "phone" in changes:
        user.phone = normalize_phone(changes["phone"])
    if "birth_date" in changes:
        user.birth_date = parse_birth_date(changes["birth_date"])


def _patient_or_404(db: Session, patient_id) -> User:
    patient = db.get(User, patient_id)
    if patient is None or patient.role != "PATIENT":
        raise Problem(404, "NOT_FOUND", "Пациент не найден")
    return patient


def _require_patient_consent(db: Session, patient_id) -> None:
    active = db.scalar(select(Consent).where(Consent.user_id == patient_id, Consent.revoked_at.is_(None)))
    if active is None:
        raise Problem(403, "CONSENT_REQUIRED", "Нет активного согласия пациента — контакты недоступны (152-ФЗ)")


def clinician_read(db: Session, clinician_id, patient_id, ip: str | None = None) -> dict:
    """Врач читает контакты закреплённого пациента: roster + consent + аудит."""
    from .clinician import require_roster

    require_roster(db, clinician_id, patient_id)
    _require_patient_consent(db, patient_id)
    patient = _patient_or_404(db, patient_id)
    audit_record(db, clinician_id, "READ_PATIENT_CONTACTS", "patient", str(patient_id), ip=ip)
    db.commit()
    return contacts_payload(patient)


def clinician_update(db: Session, clinician_id, patient_id, changes: dict, ip: str | None = None) -> dict:
    """Врач правит контакты закреплённого пациента. Аудит без ПДн в payload."""
    from .clinician import require_roster

    require_roster(db, clinician_id, patient_id)
    _require_patient_consent(db, patient_id)
    patient = _patient_or_404(db, patient_id)
    apply_contacts(patient, changes)
    audit_record(db, clinician_id, "UPDATE_PATIENT_CONTACTS", "patient", str(patient_id), ip=ip)
    db.commit()
    return contacts_payload(patient)


def own_read(db: Session, user: User, ip: str | None = None) -> dict:
    """Пациент читает собственные контакты (require_consent — на роутере)."""
    audit_record(db, user.id, "READ_OWN_CONTACTS", "user", str(user.id), ip=ip)
    db.commit()
    return contacts_payload(user)


def own_update(db: Session, user: User, changes: dict, ip: str | None = None) -> dict:
    """Пациент правит только собственные контакты."""
    apply_contacts(user, changes)
    audit_record(db, user.id, "UPDATE_OWN_CONTACTS", "user", str(user.id), ip=ip)
    db.commit()
    return contacts_payload(user)
