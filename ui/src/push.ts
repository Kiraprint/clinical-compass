// Web Push (item 3): подключение service worker + подписка врача/админа.
import { api } from "./api";

function urlBase64ToUint8Array(base64: string): BufferSource {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const normalized = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(normalized);
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr as BufferSource;
}

export async function initPush(role: string): Promise<void> {
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