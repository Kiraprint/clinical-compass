import { useEffect, useRef, useState } from "react";
import { Capacitor } from "@capacitor/core";
import { api, NotificationItem } from "./api";
import { useEscapeClose } from "./keys";
import { enableWebPush, pushChannelState, PushChannelState } from "./push";

export default function NotificationsBell() {
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [fcm, setFcm] = useState<null | { window_active: boolean; remaining_seconds: number }>(null);
  const [push, setPush] = useState<PushChannelState | null>(null);
  const [pushBusy, setPushBusy] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  const load = async () => {
    try {
      // Колокольчик — опрос «что нового»: кэш GET здесь отдал бы вчерашнее.
      api.invalidate("/api/v1/me/notifications");
      const data = await api.getNotifications();
      setItems(data.items);
      setUnread(data.unread);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const loadFcm = async () => {
    if (!Capacitor.isNativePlatform()) return;
    try {
      setFcm(await api.fcmStatus());
    } catch {
      // окно недоступно — не показываем статус
    }
  };

  const loadPush = async () => {
    if (Capacitor.isNativePlatform()) return;
    try {
      setPush(await pushChannelState());
    } catch {
      // состояние недоступно — блок канала не рисуем
    }
  };

  useEffect(() => {
    load();
    loadFcm();
    loadPush();
    const t = setInterval(() => {
      load();
      loadFcm();
    }, 30000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (open) void loadPush();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);
  // FR-47: Escape закрывает открытую панель уведомлений.
  useEscapeClose(open, () => setOpen(false));

  const markAll = async () => {
    try {
      await api.readNotifications();
      // Отметка «прочитано» — локальный факт: переписываем список из состояния,
      // без повторного GET уведомлений.
      setItems((cur) => cur.map((item) => ({ ...item, read: true })));
      setUnread(0);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  // Жест пользователя: браузер спрашивает разрешение только отсюда.
  const onEnablePush = async () => {
    setPushBusy(true);
    setError("");
    try {
      setPush(await enableWebPush());
    } catch (e) {
      // Отказ пользователя не ошибка: состояние канала ниже покажет «разрешение не выдано».
      setError(e instanceof Error ? e.message : "Не удалось включить уведомления");
    } finally {
      setPushBusy(false);
    }
  };

  const markOne = async (id: string) => {
    try {
      await api.readNotifications([id]);
      setItems((cur) => cur.map((item) => (item.id === id ? { ...item, read: true } : item)));
      const wasUnread = items.some((item) => item.id === id && !item.read);
      if (wasUnread) setUnread((cur) => Math.max(0, cur - 1));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="bell" ref={box}>
      <button className="ghost bell-btn" onClick={() => setOpen((o) => !o)} aria-label="Уведомления">
        🔔{unread > 0 && <span className="badge-badge">{unread}</span>}
      </button>
      {open && (
        <div className="bell-panel">
          <div className="bell-head">
            <b>Уведомления</b>
            <button className="ghost" onClick={markAll}>
              Прочитать все
            </button>
          </div>
          {!Capacitor.isNativePlatform() && push && (
            <div className="bell-push" aria-live="polite">
              {push.permission === "unsupported" && (
                <span className="small muted">
                  Push-уведомления недоступны: браузер не поддерживает их на этом сайте (нужен HTTPS).
                </span>
              )}
              {push.permission === "granted" && push.subscribed && (
                <span className="small muted">Уведомления включены</span>
              )}
              {push.permission === "granted" && !push.subscribed && (
                <button className="ghost" disabled={pushBusy} onClick={() => void onEnablePush()}>
                  Подключить уведомления
                </button>
              )}
              {push.permission === "default" && (
                <>
                  <span className="small muted">Браузерные уведомления не включены.</span>
                  <button className="ghost" disabled={pushBusy} onClick={() => void onEnablePush()}>
                    Включить уведомления
                  </button>
                </>
              )}
              {push.permission === "denied" && (
                <span className="small muted">
                  Уведомления заблокированы в браузере — разрешите их в настройках сайта.
                </span>
              )}
            </div>
          )}
          {error && <p className="error">{error}</p>}
          {items.length === 0 && <p style={{ color: "#5a6478", padding: 8 }}>Нет уведомлений</p>}
          {items.map((n) => (
            <div
              key={n.id}
              className={`bell-item ${n.read ? "" : "unread"}`}
              title={
                n.kind === "PATIENT_MESSAGE"
                  ? "Открыть переписку с пациентом"
                  : n.patient_id
                    ? "Открыть карточку пациента"
                    : undefined
              }
              onClick={() => {
                if (n.kind === "PATIENT_MESSAGE" && n.patient_id) {
                  // Deep-link уже поддерживает ClinicianView (FR-29).
                  window.location.hash = `chats=patient:${n.patient_id}`;
                  setOpen(false);
                } else if (n.patient_id) {
                  window.location.hash = "patient=" + n.patient_id;
                  setOpen(false);
                }
                if (!n.read) markOne(n.id);
              }}
            >
              <div className="bell-title">
                {n.title}
                {!n.read && <span className="dot" />}
              </div>
              <div className="bell-body">{n.body}</div>
              <div className="bell-time">{new Date(n.created_at).toLocaleTimeString("ru-RU")}</div>
            </div>
          ))}
          {fcm?.window_active && (
            <p className="bell-fcm">
              Push: окно активно — попробуйте через ~{Math.ceil(fcm.remaining_seconds / 60)} мин
            </p>
          )}
        </div>
      )}
    </div>
  );
}