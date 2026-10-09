import { useEffect, useState } from "react";
import { api, AuditEvent } from "../api";

export default function AdminView() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api.adminAudit().then((data) => {
      setEvents(data.items);
      setLoaded(true);
    });
  }, []);

  return (
    <div className="card">
      <h2>Журнал аудита (append-only)</h2>
      {!loaded && <p style={{ color: "#5a6478" }}>Загрузка…</p>}
      <table>
        <thead>
          <tr>
            <th>Время</th>
            <th>Действие</th>
            <th>Объект</th>
            <th>Актор</th>
            <th>IP</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.id}>
              <td>{e.occurred_at.slice(0, 19).replace("T", " ")}</td>
              <td>{e.action}</td>
              <td>
                {e.object_type} {e.object_id ?? ""}
              </td>
              <td>{e.actor_id ? e.actor_id.slice(0, 8) : "—"}</td>
              <td>{e.ip ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p style={{ color: "#8a94a8", fontSize: 12 }}>
        Журнал защищён от правки на уровне БД (триггер append-only). Привязка врач→пациент выполняется
        через симулятор/API.
      </p>
    </div>
  );
}