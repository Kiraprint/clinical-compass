# Item 3: Web Push подписки. Контракт: docs/04 §3.1.
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.services import push

router = APIRouter(tags=["push"])


class SubscribeBody(BaseModel):
    endpoint: str
    keys: dict[str, str]  # {p256dh, auth}


class UnsubscribeBody(BaseModel):
    endpoint: str


@router.get("/push/vapid-public-key")
def public_key(user: User = Depends(get_current_user)):
    """Публичный VAPID-ключ для подписки браузера (application_server_key)."""
    return {"public_key": push.vapid_public_key()}


@router.post("/push/subscribe")
def subscribe(body: SubscribeBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    push.upsert_subscription(db, user.id, body.endpoint, body.keys.get("p256dh", ""), body.keys.get("auth", ""))
    return {"subscribed": True}


@router.delete("/push/unsubscribe")
def unsubscribe(body: UnsubscribeBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    push.delete_subscription(db, user.id, body.endpoint)
    return {"subscribed": False}

# --- FCM (нативное приложение, item 4) ---------------------------------------

class DeviceTokenBody(BaseModel):
    token: str
    platform: str = "android"  # android | ios | web

@router.post("/push/device-token")
def register_device_token(
    body: DeviceTokenBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Регистрация FCM-токена устройства (идемпотентный апсерт по токену)."""
    push.upsert_device_token(db, user.id, body.token, body.platform)
    return {"registered": True}

@router.delete("/push/device-token")
def unregister_device_token(
    body: DeviceTokenBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    push.delete_device_token(db, user.id, body.token)
    return {"registered": False}

@router.get("/push/fcm/status")
def fcm_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Окно перехода на FCM: активное + сколько секунд осталось до повторной попытки."""
    return push.fcm_status(db, user.id)

@router.post("/push/fcm/upgrade")
def fcm_upgrade(
    body: DeviceTokenBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Одноразовый переход на FCM. Внутри окна (60 мин) — 429 FCM_COOLDOWN."""
    push.upgrade_to_fcm(db, user.id, body.token, body.platform)
    return {"upgraded": True}