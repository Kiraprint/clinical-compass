"""Отображение домена «протокол приёма» в конверт РЭМД и обратно (FR-41).

Конверт — метаданные `registerDocument` REST-фасада ЕГИСЗ (research §1):
идентификаторы МО/ИС/пациента, авторы, вид СЭМД НСИ 1520 и сам документ CDA R2
в base64 вместе с отсоединённой CMS-подписью. PDF и PNG в РЭМД не уходят: с
01.01.2024 принимаются только структурированные СЭМД (docs/18 §7.6).

Идемпотентность держится на `local_document_uid` — UUIDv5 от
`protocol_id:version`. Повторная отправка той же версии даёт тот же GUID и тот
же `request_id`, а изменённые байты под тем же GUID РЭМД отвергает (409
`NOT_UNIQUE_PROVIDED_ID`).

ПДн (СНИЛС, ФИО) проходят только внутри конверта: в логи и в БД они не
попадают (в `emias_deliveries` остаются sha256 байт и идентификаторы).
"""

from __future__ import annotations

import base64
import uuid
from datetime import date, datetime, timezone

from app.errors import Problem

# Виды СЭМД — подмножество НСИ 1.2.643.5.1.13.13.11.1520, которые умеет
# отправлять наш контур. 34 — «протокол консультации» (research §1); мок
# проверяет только код, семантику не выдумывает.
NSI_1520_KINDS = {
    34: "Протокол консультации",
    109: "Эпикриз",
}

DELIVERY_STATUSES = ("queued", "sending", "registered", "rejected", "failed")
TERMINAL_STATUSES = frozenset({"registered", "rejected"})
# Коды каталога НСИ, которые docs/18 §8.4 помечает «повтор: да»: такой ответ РЭМД
# не «отказ в документе», а сбой, поэтому он даёт `failed` (повторяемо), не `rejected`.
RETRYABLE_ERROR_CODES = frozenset(
    {"RATE_LIMITED", "MIS_NOT_AVAILABLE", "INTERNAL_ERROR", "INSUFFICIENT_SCOPE"}
)

_SNILS_WEIGHTS = (9, 8, 7, 6, 5, 4, 3, 2, 1)


# --- СНИЛС -------------------------------------------------------------------


def snils_digits(snils: str | None) -> str:
    """Только цифры: «112-233-445 95» → «11223344595»."""
    return "".join(ch for ch in (snils or "") if ch.isdigit())


def snils_valid(snils: str | None) -> bool:
    """11 цифр и контрольная сумма по алгоритму ПФР.

    Сумма первых девяти цифр с весами 9…1; > 101 → остаток от деления на 101;
    100 → «00». Сравнивается с последними двумя цифрами.
    """
    digits = snils_digits(snils)
    if len(digits) != 11:
        return False
    total = sum(int(d) * w for d, w in zip(digits[:9], _SNILS_WEIGHTS))
    if total > 101:
        total %= 101
    if total in (100, 101):
        # 100/101 → контрольное число «00» (алгоритм ПФР).
        total = 0
    return total == int(digits[9:])


def snils_formatted(snils: str) -> str:
    """Канонический вид для конверта: 123-456-789 00."""
    digits = snils_digits(snils)
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:9]} {digits[9:]}"


# --- идентификаторы ----------------------------------------------------------


def local_document_uid(protocol_id, version: int) -> uuid.UUID:
    """Детерминированный GUID версии протокола — ключ идемпотентности РЭМД."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"clinical-compass:protocol:{protocol_id}:v{version}")


def document_number(protocol_id, version: int) -> str:
    """Номер документа МО: без ПДн, стабильный для версии протокола."""
    return f"CC-{str(protocol_id)[:8]}-{version}"


def split_fio(fio: str | None) -> tuple[str, str, str]:
    parts = (fio or "").split()
    surname = parts[0] if parts else ""
    name = parts[1] if len(parts) > 1 else ""
    patronymic = " ".join(parts[2:]) if len(parts) > 2 else ""
    return surname, name, patronymic


# --- конверт -----------------------------------------------------------------


def build_envelope(
    *,
    cfg,
    protocol_id,
    version: int,
    document_number_value: str,
    cda: bytes,
    signature_der: bytes,
    patient_fio: str | None,
    patient_birth_date: date | None,
    patient_snils: str | None,
    personals: list[dict],
) -> dict:
    """Конверт `registerDocument`. Ничего не логирует и не печатает."""
    snils = patient_snils or cfg.patient_snils
    if not snils_valid(snils):
        raise Problem(
            409,
            "PATIENT_SNILS_INVALID",
            "СНИЛС пациента не задан или не проходит контрольную сумму",
        )
    if cfg.document_kind not in NSI_1520_KINDS:
        raise Problem(
            409,
            "UNKNOWN_DOCUMENT_KIND",
            f"Вид СЭМД {cfg.document_kind} не поддерживается контуром",
        )
    surname, name, patronymic = split_fio(patient_fio)
    payload = {
        "lpu_uid": cfg.lpu_uid,
        "local_document_uid": str(local_document_uid(protocol_id, version)),
        "document_number": document_number_value,
        "kind": cfg.document_kind,
        "department": {"local_oid": cfg.department_oid, "name": cfg.department_name},
        "patient": {
            "local_id": str(protocol_id),
            "surname": surname,
            "name": name,
            "patr_name": patronymic,
            "birth_date": patient_birth_date.isoformat() if patient_birth_date else None,
            "snils": snils_formatted(snils),
        },
        "personals": personals,
        "cda": base64.b64encode(cda).decode("ascii"),
        "sig": base64.b64encode(signature_der).decode("ascii"),
        "callback_url": cfg.callback_url,
    }
    return payload


# --- callback ----------------------------------------------------------------


def callback_outcome(payload: dict) -> dict:
    """Разобрать `registerDocumentResult` в терминальное состояние доставки.

    Возвращает `{status, external_id, error_code, error_message, result}`.
    `status:"success"` без `data.emdr_id` — это ошибка протокола обмена, а не
    успех: без emdr_id документ не зарегистрирован.
    """
    status = payload.get("status")
    errors = payload.get("errors") or []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    result = {
        "message_type": payload.get("message_type"),
        "status": status,
        "emdr_id": (payload.get("data") or {}).get("emdr_id"),
        "errors": errors[:5],
    }
    if status == "success":
        emdr_id = (payload.get("data") or {}).get("emdr_id")
        if not emdr_id:
            return {
                "status": "rejected",
                "external_id": None,
                "error_code": "VALIDATION_ERROR",
                "error_message": "РЭМД вернул success без emdr_id",
                "result": result,
            }
        return {
            "status": "registered",
            "external_id": str(emdr_id),
            "error_code": None,
            "error_message": None,
            "result": result,
        }
    code = str(first.get("code") or "INTERNAL_ERROR")[:64]
    return {
        "status": "failed" if code in RETRYABLE_ERROR_CODES else "rejected",
        "external_id": None,
        "error_code": code,
        "error_message": str(first.get("message") or "РЭМД отклонил документ")[:400],
        "result": result,
    }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
