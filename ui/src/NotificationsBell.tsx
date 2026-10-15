import { useEffect, useRef, useState } from "react";
import { api, NotificationItem } from "./api";

export default function NotificationsBell() {
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const box = useRef<HTMLDivElement>(null);

  const load = async () => {
    try {
      const data = await api.getNotifications();
      setItems(data.items);
      setUnread(data.unread);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const markAll = async () => {
    try {
      await api.readNotifications();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const markOne = async (id: string) => {
    try {
      await api.readNotifications([id]);
      await load();
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
          {error && <p className="error">{error}</p>}
          {items.length === 0 && <p style={{ color: "#5a6478", padding: 8 }}>Нет уведомлений</p>}
          {items.map((n) => (
            <div
              key={n.id}
              className={`bell-item ${n.read ? "" : "unread"}`}
              onClick={() => !n.read && markOne(n.id)}
            >
              <div className="bell-title">
                {n.title}
                {!n.read && <span className="dot" />}
              </div>
              <div className="bell-body">{n.body}</div>
              <div className="bell-time">{new Date(n.created_at).toLocaleTimeString("ru-RU")}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}