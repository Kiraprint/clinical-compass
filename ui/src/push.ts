// Push (items 3-4): Web Push для веб-клиента + FCM для нативного приложения (Capacitor).
//
//  - Веб (браузер): service worker + VAPID-подписка (только CLINICIAN/ADMIN),
//    endpoint регистрируется в /api/v1/push/subscribe.
//  - Нативно (Android APK): плагин @capacitor/push-notifications → FCM-токен,
//    регистрируется в /api/v1/push/device-token и апгрейдится через
//    /api/v1/push/fcm/upgrade (одноразовое окно, 429 FCM_COOLDOWN внутри 60 мин).
//
// Разрешение браузера НЕ запрашивается автоматически: Chrome/Firefox без жеста
// пользователя молча отклоняют запрос. Колокол показывает состояние канала и
// просит разрешение только по явной кнопке (`enableWebPush`).
import { Capacitor } from "@capacitor/core";
import { PushNotifications } from "@capacitor/push-notifications";
import { api } from "./api";

export type WebPushPermission = "granted" | "default" | "denied" | "unsupported";

export interface PushChannelState {
  /** false — нет secure context / serviceWorker / Notification (push недоступен). */
  supported: boolean;
  permission: WebPushPermission;
  /** Есть активная браузерная подписка и она зарегистрирована на бэкенде. */
  subscribed: boolean;
}

function urlBase64ToUint8Array(base64: string): BufferSource {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const normalized = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(normalized);
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr as BufferSource;
}

export function webPushSupported(): boolean {
  return (
    !Capacitor.isNativePlatform() &&
    "serviceWorker" in navigator &&
    "Notification" in window &&
    typeof Notification !== "undefined"
  );
}

function webPermission(): WebPushPermission {
  return webPushSupported() ? (Notification.permission as WebPushPermission) : "unsupported";
}

async function registerWorker(): Promise<ServiceWorkerRegistration | null> {
  try {
    return await navigator.serviceWorker.register("/sw.js");
  } catch (e) {
    console.warn("service worker registration failed:", e);
    return null;
  }
}

/** Подписка существующего разрешения: без запроса permission. */
async function ensureSubscription(reg: ServiceWorkerRegistration): Promise<boolean> {
  if (!reg.pushManager) return false;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    const { public_key } = await api.vapidPublicKey();
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(public_key),
    });
  }
  const json = sub.toJSON() as PushSubscriptionJSON;
  await api.subscribePush(sub.endpoint, {
    p256dh: json.keys?.p256dh ?? "",
    auth: json.keys?.auth ?? "",
  });
  return true;
}

/** Текущее состояние канала — без каких-либо запросов разрешения. */
export async function pushChannelState(): Promise<PushChannelState> {
  const permission = webPermission();
  if (permission === "unsupported") {
    return { supported: false, permission, subscribed: false };
  }
  try {
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = reg?.pushManager ? await reg.pushManager.getSubscription() : null;
    return { supported: true, permission, subscribed: sub !== null };
  } catch {
    return { supported: true, permission, subscribed: false };
  }
}

/**
 * Явное включение из жеста пользователя: разрешение → подписка → регистрация.
 * Возвращает состояние после попытки (denied остаётся denied — браузер не
 * спросит повторно, это надо чинить в настройках сайта).
 */
export async function enableWebPush(): Promise<PushChannelState> {
  if (!webPushSupported()) return pushChannelState();
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return pushChannelState();
  const reg = await registerWorker();
  if (!reg) return { supported: true, permission: "granted", subscribed: false };
  try {
    const subscribed = await ensureSubscription(reg);
    return { supported: true, permission: "granted", subscribed };
  } catch (e) {
    console.warn("push subscribe failed:", e);
    return { supported: true, permission: "granted", subscribed: false };
  }
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

/**
 * Инициализация после логина. Веб: подписывает только при уже выданном
 * разрешении; `default` оставляет колоколу (жест пользователя).
 */
export async function initPush(role: string): Promise<void> {
  if (Capacitor.isNativePlatform()) {
    await initNativeFcm(role);
    return;
  }
  if (role !== "CLINICIAN" && role !== "ADMIN") return; // пуши — только персоналу
  if (!webPushSupported()) return;
  if (webPermission() !== "granted") return; // без жеста не спрашиваем
  try {
    const reg = await registerWorker();
    if (!reg) return;
    await ensureSubscription(reg);
  } catch (e) {
    console.warn("push init failed:", e);
  }
}
