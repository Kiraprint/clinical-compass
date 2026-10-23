"""Уведомления и Web Push (item 3).

- Уведомления: строки Notification (внутри транзакции финиша скрининга).
- Web Push: VAPID-подписи; доставка best-effort (сбой push-сервиса не роняет API).

Ключи VAPID задаются env CC_VAPID_PRIVATE_KEY (публичный выводится из приватного).
Демо-ключи генерируются один раз на старте и держатся в памяти —
при перезапуске контейнера подписки браузера могут стать недействительными
(для демо приемлемо; в проде — статичные ключи в секретах инсталляции).
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import Problem
from app.models import (
    ClinicianRoster,
    DeviceToken,
    Escalation,
    FcmState,
    Notification,
    PushSubscription,
    User,
)

log = logging.getLogger(__name__)

FCM_COOLDOWN_SECONDS = 3600  # окно «попробуйте через час» после одноразового апгрейда

# --- VAPID-ключи -------------------------------------------------------------

_vapid_cache: dict[str, object] | None = None


def _load_vapid() -> dict[str, object]:
    """Возвращает {private_pem, public_b64, vapid} (env или сгенерированные)."""
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
    raw = sk.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    # pywebpush трактует строку в vapid_private_key как ПУТЬ К ФАЙЛУ, а не как PEM:
    # передаём готовый объект Vapid, иначе подпись падает на разборе ключа.
    from py_vapid import Vapid

    _vapid_cache = {
        "private_pem": private_pem,
        "public_b64": base64.urlsafe_b64encode(raw).rstrip(b"=").decode(),
        "vapid": Vapid.from_pem(private_pem.encode()),
    }
    if not settings.vapid_private_key:
        log.warning("VAPID-ключи не заданы env — сгенерированы в памяти (демо)")
    return _vapid_cache


def vapid_public_key() -> str:
    return str(_load_vapid()["public_b64"])


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

# --- FCM-токены нативного приложения (item 4) --------------------------------

def upsert_device_token(db: Session, user_id: uuid.UUID, token: str, platform: str) -> None:
    """Регистрация FCM-токена (один на устройство, апсерт по токену)."""
    row = db.scalar(select(DeviceToken).where(DeviceToken.token == token))
    if row is None:
        db.add(DeviceToken(user_id=user_id, token=token, platform=platform))
    else:
        row.user_id = user_id
        row.platform = platform
    db.commit()

def delete_device_token(db: Session, user_id: uuid.UUID, token: str) -> None:
    row = db.scalar(
        select(DeviceToken).where(DeviceToken.token == token, DeviceToken.user_id == user_id)
    )
    if row is not None:
        db.delete(row)
        db.commit()

def _fcm_remaining(db: Session, user_id: uuid.UUID) -> int:
    """Оставшиеся секунды cooldown-окна (0 = можно апгрейдить)."""
    state = db.get(FcmState, user_id)
    if state is None or state.last_upgraded_at is None:
        return 0
    delta = datetime.now(timezone.utc) - state.last_upgraded_at
    return max(0, FCM_COOLDOWN_SECONDS - int(delta.total_seconds()))

def fcm_status(db: Session, user_id: uuid.UUID) -> dict:
    """Статус окна перехода на FCM: активное окно + сколько осталось секунд."""
    remaining = _fcm_remaining(db, user_id)
    return {"window_active": remaining > 0, "remaining_seconds": remaining}

def upgrade_to_fcm(db: Session, user_id: uuid.UUID, token: str, platform: str) -> None:
    """Одноразовый переход на FCM-канал (item 4).

    Внутри окна (60 мин после успешного апгрейда) — 429 FCM_COOLDOWN.
    Апгрейд идемпотентен по токену (апсерт), поэтому повторная подписка
    на то же устройство после окна не создаёт дублей.
    """
    state = db.get(FcmState, user_id, with_for_update=True)
    remaining = _fcm_remaining(db, user_id)
    if remaining > 0:
        raise Problem(429, "FCM_COOLDOWN", f"Попробуйте через {remaining // 60 + 1} мин")
    if state is None:
        state = FcmState(user_id=user_id)
        db.add(state)
    state.last_upgraded_at = datetime.now(timezone.utc)
    row = db.scalar(select(DeviceToken).where(DeviceToken.token == token))
    if row is None:
        db.add(DeviceToken(user_id=user_id, token=token, platform=platform))
    else:
        row.user_id = user_id
        row.platform = platform
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
    if escalation.kind == "SUICIDAL":
        recipient_kind, title = "суицидальный риск", "RED-эскалация"
    elif escalation.kind == "CHECKIN_RISK_URGENT":
        # Срочный сигнал: HR4/RS требуют реакции сегодня (docs/16 §3).
        recipient_kind, title = "срочный сигнал риска в чек-ине", "СРОЧНО: сигнал риска чек-ина"
    elif escalation.kind == "CHECKIN_RISK":
        # docs/16 §3: gate/RS-сигнал чек-ина — очередь клинического рассмотрения,
        # а не валидированная категория риска.
        recipient_kind, title = "сигнал риска в ежедневном чек-ине", "Сигнал риска чек-ина"
    else:
        recipient_kind, title = "тяжёлое состояние", "RED-эскалация"
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


# --- Доставка push (best-effort) ---------------------------------------------

_fcm_sa_cache: dict | None = None

def _fcm_service_account() -> dict | None:
    """Service account Firebase из env CC_FCM_SERVICE_ACCOUNT_B64 (base64 JSON)."""
    global _fcm_sa_cache
    if _fcm_sa_cache is not None:
        return _fcm_sa_cache or None
    if not settings.fcm_service_account_b64:
        _fcm_sa_cache = {}
        return None
    try:
        import json

        sa = json.loads(base64.b64decode(settings.fcm_service_account_b64).decode())
        _fcm_sa_cache = sa
        return sa
    except Exception as exc:  # noqa: BLE001
        log.warning("не удалось распарсить CC_FCM_SERVICE_ACCOUNT_B64: %s", exc)
        _fcm_sa_cache = {}
        return None

def _fcm_access_token(sa: dict) -> str | None:
    """OAuth2-токен для FCM HTTP v1 (подпись RS256 через PyJWT + cryptography)."""
    import time

    import jwt

    now = int(time.time())
    claims = {
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/firebase.messaging",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }
    try:
        assertion = jwt.encode(claims, sa["private_key"], algorithm="RS256")
    except Exception as exc:  # noqa: BLE001
        log.warning("FCM: не удалось подписать JWT: %s", exc)
        return None
    try:
        import urllib.parse
        import urllib.request

        body = urllib.parse.urlencode(
            {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
        ).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json

            return json.load(resp)["access_token"]
    except Exception as exc:  # noqa: BLE001
        log.warning("FCM: не удалось получить access_token: %s", exc)
        return None

def _fcm_send(token: str, title: str, body: str) -> bool:
    """Отправка одного FCM-сообщения. True = доставлено/принято; False = сбой."""
    sa = _fcm_service_account()
    if sa is None:
        return False
    access = _fcm_access_token(sa)
    if not access:
        return False
    try:
        import json
        import urllib.request

        payload = json.dumps(
            {"message": {"token": token, "notification": {"title": title, "body": body}}}
        ).encode()
        req = urllib.request.Request(
            f"https://fcm.googleapis.com/v1/projects/{sa['project_id']}/messages:send",
            data=payload,
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:  # noqa: BLE001
        log.warning("FCM send error %s (token %s...): %s", exc.code, token[:40], exc.read()[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("FCM send сбой (token %s...): %s", token[:40], exc)
    return False

def push_to_clinicians(
    db: Session, clinician_ids: list[uuid.UUID], title: str, body: str
) -> None:
    """Расслылка pushes врачам (Web Push + FCM). Сбои не пробрасываются (best-effort)."""
    if not settings.push_enabled or not clinician_ids:
        return
    try:
        keys = _load_vapid()
        subs = db.scalars(
            select(PushSubscription).where(PushSubscription.user_id.in_(clinician_ids))
        ).all()
        payload = json.dumps({"title": title, "body": body})
        from pywebpush import WebPushException, webpush

        for s in subs:
            try:
                webpush(
                    {"endpoint": s.endpoint, "keys": {"p256dh": s.p256dh, "auth": s.auth}},
                    data=payload,
                    vapid_private_key=keys["vapid"],
                    vapid_claims={"sub": "mailto:admin@compass.demo"},
                    timeout=5,
                )
            except WebPushException as exc:
                log.warning("push не доставлен (%s): %s", s.endpoint[:60], exc)
            except Exception as exc:  # noqa: BLE001 — одна плохая подписка не валит остальные
                log.warning("push error %s: %s", s.endpoint[:60], exc)

        # Нативные FCM-токены (Android APK).
        devices = db.scalars(
            select(DeviceToken).where(DeviceToken.user_id.in_(clinician_ids))
        ).all()
        for d in devices:
            _fcm_send(d.token, title, body)
    except Exception as exc:  # noqa: BLE001
        log.warning("push_to_clinicians сбой: %s", exc)