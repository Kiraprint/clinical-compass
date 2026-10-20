// Push (items 3-4): Web Push для веб-клиента + FCM для нативного приложения (Capacitor).
//
//  - Веб (браузер): service worker + VAPID-подписка (только CLINICIAN/ADMIN),
//    endpoint регистрируется в /api/v1/push/subscribe.
//  - Нативно (Android APK): плагин @capacitor/push-notifications → FCM-токен,
//    регистрируется в /api/v1/push/device-token и апгрейдится через
//    /api/v1/push/fcm/upgrade (одноразовое окно, 429 FCM_COOLDOWN внутри 60 мин).
import { Capacitor } from "@capacitor/core";
import { PushNotifications } from "@capacitor/push-notifications";
import { api } from "./api";

function urlBase64ToUint8Array(base64: string): BufferSource {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const normalized = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(normalized);
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr as BufferSource;
}

async function initNativeFcm(role: string): Promise<void> {
  // Нативный FCM-канал: токен устройства → backend.
  if (role !== "CLINICIAN" && role !== "ADMIN") return; // пуши — только персоналу
  try {
    const check = await PushNotifications.checkPermissions();
    if (check.receive !== "granted") {
      const req = await PushNotifications.requestPermissions();
      if (req.receive !== "granted") return;
    }
    await PushNotifications.register();
    await PushNotifications.addListener("registration", (data) => {
      const token = data.value;
      void api.registerDeviceToken(token);
      // Апгрейд на FCM: внутри окна вернёт 429 FCM_COOLDOWN (обработано в колоколе статусом).
      void api.fcmUpgrade(token).catch(() => undefined);
    });
  } catch (e) {
    console.warn("native push init failed:", e);
  }
}

export async function initPush(role: string): Promise<void> {
  if (Capacitor.isNativePlatform()) {
    await initNativeFcm(role);
    return;
  }
  if (!("serviceWorker" in navigator) || !("Notification" in window)) return;
  if (role !== "CLINICIAN" && role !== "ADMIN") return; // пуши — только персоналу
  try {
    const reg = await navigator.serviceWorker.register("/sw.js");
    if (!reg.pushManager) return;
    const permission = await Notification.requestPermission();
    if (permission !== "granted") return;
    const { public_key } = await api.vapidPublicKey();
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(public_key),
    });
    const json = sub.toJSON() as PushSubscriptionJSON;
    await api.subscribePush(sub.endpoint, {
      p256dh: json.keys?.p256dh ?? "",
      auth: json.keys?.auth ?? "",
    });
  } catch (e) {
    console.warn("push init failed:", e);
  }
}
