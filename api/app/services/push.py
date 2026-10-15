"""Уведомления и Web Push (item 3).

- Уведомления: строки Notification (внутри транзакции финиша скрининга).
- Web Push: VAPID-подписи; доставка best-effort (сбой push-сервиса не роняет API).

Ключи VAPID задаются env CC_VAPID_PRIVATE_KEY / CC_VAPID_PUBLIC_KEY.
Демо-ключи генерируются один раз на старте и держатся в памяти —
при перезапуске контейнера подписки браузера могут стать недействительными
(для демо приемлемо; в проде — статичные ключи в секретах инсталляции).
"""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import Problem
from app.models import ClinicianRoster, Escalation, Notification, PushSubscription, User

log = logging.getLogger(__name__)

# --- VAPID-ключи -------------------------------------------------------------

_vapid_cache: dict[str, str] | None = None


def _load_vapid() -> dict[str, str]:
    """Возвращает {private_pem, public_pem, public_b64} (env или сгенерированные)."""
    global _vapid_cache
    if _vapid_cache is not None:
        return _vapid_cache

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if settings.vapid_private_key:
        # base64(PKCS8 PEM). Публичный ключ и b64 выводим из приватного.
        try:
            pem = base64.b64decode(settings.vapid_private_key).decode()
        except Exception:
            pem = settings.vapid_private_key  # уже PEM строкой — на случай прямого env
        sk = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(sk, ec.EllipticCurvePrivateKey):
            raise ValueError("VAPID-ключ должен быть EC P-256 (SECP256R1)")
    else:
        sk = ec.generate_private_key(ec.SECP256R1())

    private_pem = sk.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = sk.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    raw = sk.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    _vapid_cache = {
        "private_pem": private_pem,
        "public_pem": public_pem,
        "public_b64": base64.urlsafe_b64encode(raw).rstrip(b"=").decode(),
    }
    if not settings.vapid_private_key:
        log.warning("VAPID-ключи не заданы env — сгенерированы в памяти (демо)")
    return _vapid_cache


def vapid_public_key() -> str:
    return _load_vapid()["public_b64"]


# --- Подписки -----------------------------------------------------------------


def upsert_subscription(db: Session, user_id: uuid.UUID, endpoint: str, p256dh: str, auth: str) -> None:
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if sub is None:
        sub = PushSubscription(user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth)
        db.add(sub)
    else:
        sub.user_id = user_id
        sub.p256dh = p256dh
        sub.auth = auth
    db.commit()


def delete_subscription(db: Session, user_id: uuid.UUID, endpoint: str) -> None:
    sub = db.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint, PushSubscription.user_id == user_id)
    )
    if sub is not None:
        db.delete(sub)
        db.commit()


# --- Уведомления -------------------------------------------------------------


def notify_escalation_clinicians(
    db: Session, patient_id: uuid.UUID, escalation: Escalation
) -> list[uuid.UUID]:
    """Создаёт Notification для врачей из roster пациента и возвращает их user_id.

    Вызывается внутри транзакции финиша скрининга; коммитит внешний код.
    """
    patient = db.get(User, patient_id)
    patient_name = patient.fio or patient.email if patient else "пациент"
    rows = db.scalars(
        select(ClinicianRoster.clinician_id).where(ClinicianRoster.patient_id == patient_id)
    ).all()
    recipient_kind = "суицидальный риск" if escalation.kind == "SUICIDAL" else "тяжёлое состояние"
    title = "RED-эскалация"
    body = f"{patient_name}: {recipient_kind} — см. карточку пациента"
    for clinician_id in rows:
        db.add(
            Notification(
                user_id=clinician_id,
                kind="RED_ESCALATION",
                title=title,
                body=body,
                patient_id=patient_id,
                escalation_id=escalation.id,
            )
        )
    return list(rows)


def list_notifications(db: Session, user_id: uuid.UUID, limit: int = 50) -> dict:
    rows = db.scalars(
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
    ).all()
    unread = db.scalar(
        select(Notification.id)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .limit(1)
    )
    return {
        "items": [
            {
                "id": str(n.id),
                "kind": n.kind,
                "title": n.title,
                "body": n.body,
                "patient_id": str(n.patient_id) if n.patient_id else None,
                "escalation_id": str(n.escalation_id) if n.escalation_id else None,
                "read": n.read_at is not None,
                "created_at": n.created_at.isoformat(),
            }
            for n in rows
        ],
        "unread": int(unread is not None),
    }


def mark_read(db: Session, user_id: uuid.UUID, ids: list[str] | None) -> None:
    now = datetime.now(timezone.utc)
    q = select(Notification).where(Notification.user_id == user_id)
    if ids:
        parsed = []
        for i in ids:
            try:
                parsed.append(uuid.UUID(i))
            except ValueError:
                raise Problem(422, "VALIDATION", "Некорректный id уведомления")
        q = q.where(Notification.id.in_(parsed))
    else:
        q = q.where(Notification.read_at.is_(None))
    for n in db.scalars(q):
        n.read_at = now
    db.commit()


# --- Web Push (best-effort) --------------------------------------------------


def push_to_clinicians(
    db: Session, clinician_ids: list[uuid.UUID], title: str, body: str
) -> None:
    """Расслылка Web Push врачам. Сбои не пробрасываются (best-effort)."""
    if not settings.push_enabled or not clinician_ids:
        return
    try:
        keys = _load_vapid()
        subs = db.scalars(
            select(PushSubscription).where(PushSubscription.user_id.in_(clinician_ids))
        ).all()
        payload = '{"title": %r, "body": %r}' % (title, body)
        from pywebpush import WebPushException, webpush

        for s in subs:
            try:
                webpush(
                    {"endpoint": s.endpoint, "keys": {"p256dh": s.p256dh, "auth": s.auth}},
                    data=payload,
                    vapid_private_key=keys["private_pem"],
                    vapid_claims={"sub": "mailto:admin@compass.demo"},
                    timeout=5,
                )
            except WebPushException as exc:
                log.warning("push не доставлен (%s): %s", s.endpoint[:60], exc)
            except Exception as exc:  # noqa: BLE001 — одна плохая подписка не валит остальные
                log.warning("push error %s: %s", s.endpoint[:60], exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("push_to_clinicians сбой: %s", exc)