import { useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  Appointment,
  AppointmentChannel,
  AppointmentStatus,
  ClinicalSession,
  PatientCard,
} from "../api";
import SessionPanel from "./SessionPanel";

/**
 * FR-38: календарь приёмов врача (docs/18 §4.1).
 *
 * Сетка месяца 7×6 на обычном `Date` (библиотек дат в проекте нет), список дня и
 * форма записи. Пересечение у врача сервер отклоняет 409 SLOT_CONFLICT — показываем
 * detail строкой в форме, не алертом. Начатый приём открывает панель сессии
 * (SessionPanel), она же — для приёма без записи (POST /clinician/sessions).
 */

const CHANNEL_LABELS: Record<AppointmentChannel, string> = {
  in_person: "В кабинете",
  online: "Онлайн",
  phone: "Телефон",
};
const STATUS_LABELS: Record<AppointmentStatus, string> = {
  planned: "Запланирован",
  confirmed: "Подтверждён",
  completed: "Завершён",
  cancelled: "Отменён",
  no_show: "Неявка",
};
/** §4.1: длительность приёма по умолчанию (50 мин) и допустимые варианты. */
const DURATIONS = [30, 45, 50, 60, 90];
const CHANNELS: AppointmentChannel[] = ["in_person", "online", "phone"];
/** Терминальные статусы: перепланировать/начать нельзя (сервер → 409). */
const TERMINAL: AppointmentStatus[] = ["completed", "cancelled", "no_show"];
const GRID_DAYS = 42;

const TIME_FMT = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
const MONTH_FMT = new Intl.DateTimeFormat("ru-RU", { month: "long", year: "numeric" });
const DAY_FMT = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", weekday: "long" });
// Заголовки недели с понедельника: 2024-01-01 — понедельник.
const WEEKDAY_FMT = new Intl.DateTimeFormat("ru-RU", { weekday: "short" });
const WEEKDAYS = Array.from({ length: 7 }, (_, i) =>
  WEEKDAY_FMT.format(new Date(2024, 0, 1 + i)),
);

const errText = (e: unknown): string =>
  e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e);

const addDays = (d: Date, n: number) =>
  new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const ymd = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const sameDay = (a: Date, b: Date) => ymd(a) === ymd(b);

/** Первая ячейка сетки — понедельник недели, содержащей 1-е число месяца. */
function gridStart(month: Date): Date {
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const offset = (first.getDay() + 6) % 7; // 0 = понедельник
  return addDays(first, -offset);
}

/** Окно запроса — кусками по 31 дню: сервер отдаёт ≤ 200 строк на страницу (§4). */
async function fetchWindow<T>(
  fn: (from: string, to: string) => Promise<{ items: T[] }>,
  from: Date,
  to: Date,
): Promise<T[]> {
  const calls: Promise<{ items: T[] }>[] = [];
  for (let start = from; start < to; start = addDays(start, 31)) {
    const next = addDays(start, 31);
    calls.push(fn(start.toISOString(), (next > to ? to : next).toISOString()));
  }
  return (await Promise.all(calls)).flatMap((page) => page.items);
}

export default function ScheduleView() {
  // «Сегодня» фиксируем на монтировании: полночь посреди приёма не должна прыгать.
  const [today] = useState(() => {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), now.getDate());
  });
  const [month, setMonth] = useState(() => new Date(today.getFullYear(), today.getMonth(), 1));
  const [selected, setSelected] = useState(today);
  const [items, setItems] = useState<Appointment[]>([]);
  const [sessions, setSessions] = useState<ClinicalSession[]>([]);
  const [patients, setPatients] = useState<PatientCard[]>([]);
  const [rosterError, setRosterError] = useState("");
  const [version, setVersion] = useState(0);
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  const [session, setSession] = useState<ClinicalSession | null>(null);

  const gridFrom = useMemo(() => gridStart(month), [month]);
  const days = useMemo(
    () => Array.from({ length: GRID_DAYS }, (_, i) => addDays(gridFrom, i)),
    [gridFrom],
  );

  useEffect(() => {
    let cancelled = false;
    const to = addDays(gridFrom, GRID_DAYS);
    setLoading(true);
    Promise.all([
      fetchWindow(api.clinicianAppointments, gridFrom, to),
      fetchWindow(api.clinicianSessions, gridFrom, to),
    ])
      .then(([apps, sess]) => {
        if (cancelled) return;
        setItems(apps);
        setSessions(sess);
        setLoadError("");
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(errText(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [gridFrom, version]);

  // Пациенты для записи — весь roster врача, без фильтра риска вкладки «Пациенты».
  useEffect(() => {
    let cancelled = false;
    api
      .clinicianPatients()
      .then((r) => {
        if (cancelled) return;
        setPatients(r.items);
        setRosterError("");
      })
      .catch((e: unknown) => {
        if (!cancelled) setRosterError(errText(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const byDay = useMemo(() => {
    const map = new Map<string, Appointment[]>();
    for (const a of items) {
      const key = ymd(new Date(a.starts_at));
      const list = map.get(key);
      if (list) list.push(a);
      else map.set(key, [a]);
    }
    return map;
  }, [items]);

  const dayItems = byDay.get(ymd(selected)) ?? [];

  const shiftMonth = (delta: number) => {
    const next = new Date(month.getFullYear(), month.getMonth() + delta, 1);
    setMonth(next);
    setSelected(next);
  };
  const pickDay = (d: Date) => {
    setSelected(d);
    setDate(ymd(d));
    if (d.getMonth() !== month.getMonth() || d.getFullYear() !== month.getFullYear()) {
      setMonth(new Date(d.getFullYear(), d.getMonth(), 1));
    }
  };

  // --- форма новой записи ---------------------------------------------------
  const [patientId, setPatientId] = useState("");
  const [date, setDate] = useState(ymd(today));
  const [time, setTime] = useState("10:00");
  const [duration, setDuration] = useState(50);
  const [channel, setChannel] = useState<AppointmentChannel>("in_person");
  const [location, setLocation] = useState("");
  const [formErr, setFormErr] = useState("");
  const [formSaved, setFormSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!patientId && patients.length > 0) setPatientId(patients[0].patient_id);
  }, [patients, patientId]);

  const create = async () => {
    if (!patientId) {
      setFormErr("Выберите пациента");
      return;
    }
    const startsAt = new Date(`${date}T${time || "00:00"}:00`);
    if (Number.isNaN(startsAt.getTime())) {
      setFormErr("Укажите дату и время приёма");
      return;
    }
    setBusy(true);
    setFormErr("");
    setFormSaved(false);
    try {
      await api.createAppointment({
        patient_id: patientId,
        starts_at: startsAt.toISOString(),
        duration_min: duration,
        channel,
        location: location.trim() || null,
      });
      setFormSaved(true);
      pickDay(startsAt); // запись могла уйти на другой день — показываем её же в списке
      setVersion((v) => v + 1);
    } catch (e: unknown) {
      setFormErr(errText(e));
    } finally {
      setBusy(false);
    }
  };

  // --- правка записи --------------------------------------------------------
  const [editing, setEditing] = useState<string | null>(null);
  const [editTime, setEditTime] = useState("");
  const [editDuration, setEditDuration] = useState(50);
  const [editChannel, setEditChannel] = useState<AppointmentChannel>("in_person");
  const [editLocation, setEditLocation] = useState("");
  const [editStatus, setEditStatus] = useState<AppointmentStatus>("planned");
  const [rowErr, setRowErr] = useState("");

  const openEdit = (a: Appointment) => {
    const at = new Date(a.starts_at);
    setEditing(a.id);
    setEditTime(`${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`);
    setEditDuration(a.duration_min);
    setEditChannel(a.channel);
    setEditLocation(a.location ?? "");
    setEditStatus(a.status);
    setRowErr("");
  };

  const saveEdit = async (a: Appointment) => {
    setRowErr("");
    const at = new Date(a.starts_at);
    const [hh, mm] = editTime.split(":");
    const startsAt = new Date(
      at.getFullYear(),
      at.getMonth(),
      at.getDate(),
      Number(hh || 0),
      Number(mm || 0),
    );
    try {
      await api.patchAppointment(a.id, {
        starts_at: startsAt.toISOString(),
        duration_min: editDuration,
        channel: editChannel,
        location: editLocation.trim() || null,
        status: editStatus,
      });
      setEditing(null);
      setVersion((v) => v + 1);
    } catch (e: unknown) {
      setRowErr(errText(e));
    }
  };

  const cancel = async (a: Appointment) => {
    setRowErr("");
    try {
      await api.patchAppointment(a.id, { status: "cancelled" });
      setVersion((v) => v + 1);
    } catch (e: unknown) {
      setRowErr(errText(e));
    }
  };

  const start = async (a: Appointment) => {
    setRowErr("");
    try {
      setSession(await api.startAppointment(a.id));
    } catch (e: unknown) {
      setRowErr(errText(e));
    }
  };

  // --- приём без записи -----------------------------------------------------
  const [walkIn, setWalkIn] = useState(false);
  const [walkPatient, setWalkPatient] = useState("");
  const [walkChannel, setWalkChannel] = useState<AppointmentChannel>("in_person");

  const startWalkIn = async () => {
    if (!walkPatient) {
      setRowErr("Выберите пациента");
      return;
    }
    setRowErr("");
    try {
      setSession(await api.createSession({ patient_id: walkPatient, channel: walkChannel }));
    } catch (e: unknown) {
      setRowErr(errText(e));
    }
  };

  if (session) {
    return (
      <SessionPanel
        session={session}
        onClose={() => {
          setSession(null);
          setVersion((v) => v + 1);
        }}
      />
    );
  }

  return (
    <div>
      <div className="card">
        <div className="schedule-head">
          <h2>Расписание</h2>
          <div className="row">
            <button className="ghost" aria-label="Предыдущий месяц" onClick={() => shiftMonth(-1)}>
              ‹
            </button>
            <span className="schedule-month">{MONTH_FMT.format(month)}</span>
            <button className="ghost" aria-label="Следующий месяц" onClick={() => shiftMonth(1)}>
              ›
            </button>
            <button
              className="ghost"
              onClick={() => {
                setMonth(new Date(today.getFullYear(), today.getMonth(), 1));
                pickDay(today);
              }}
            >
              Сегодня
            </button>
          </div>
        </div>
        {loadError && (
          <div className="alert-danger" role="alert">
            <b>Не удалось загрузить календарь.</b> <span className="small">{loadError}</span>
          </div>
        )}
        <div className="cal-grid">
          {WEEKDAYS.map((w) => (
            <div key={w} className="cal-weekday">
              {w}
            </div>
          ))}
          {days.map((d) => {
            const list = byDay.get(ymd(d)) ?? [];
            const classes = [
              "cal-day",
              sameDay(d, selected) ? "selected" : "",
              sameDay(d, today) ? "today" : "",
              d.getMonth() !== month.getMonth() ? "other-month" : "",
            ]
              .filter(Boolean)
              .join(" ");
            return (
              <button
                key={ymd(d)}
                type="button"
                className={classes}
                aria-label={`${DAY_FMT.format(d)}${list.length ? `, приёмов: ${list.length}` : ""}`}
                onClick={() => pickDay(d)}
              >
                <span className="cal-num">{d.getDate()}</span>
                {list.slice(0, 2).map((a) => (
                  <span key={a.id} className={`cal-item ${a.status}`}>
                    {TIME_FMT.format(new Date(a.starts_at))} {a.patient_fio ?? "—"}
                  </span>
                ))}
                {list.length > 2 && <span className="cal-more">ещё {list.length - 2}</span>}
              </button>
            );
          })}
        </div>
        {loading && <p className="small muted">Загрузка…</p>}
      </div>

      <div className="card">
        <h2>{DAY_FMT.format(selected)}</h2>
        {rowErr && (
          <p className="error" role="alert">
            {rowErr}
          </p>
        )}
        {dayItems.length === 0 ? (
          <p className="small muted">Приёмов нет.</p>
        ) : (
          <div className="table-wrap">
            <table>
            <thead>
              <tr>
                <th>Время</th>
                <th>Пациент</th>
                <th>Канал</th>
                <th>Длит.</th>
                <th>Статус</th>
                <th>Действия</th>
              </tr>
            </thead>
            <tbody>
              {dayItems.map((a) => {
                const active = !TERMINAL.includes(a.status);
                return [
                  <tr key={a.id} data-appointment={a.id}>
                    <td>{TIME_FMT.format(new Date(a.starts_at))}</td>
                    <td>{a.patient_fio ?? "—"}</td>
                    <td>
                      {CHANNEL_LABELS[a.channel]}
                      {a.location ? <span className="small muted"> · {a.location}</span> : null}
                    </td>
                    <td>{a.duration_min} мин</td>
                    <td>{STATUS_LABELS[a.status]}</td>
                    <td>
                      <div className="row">
                        {a.has_session ? (
                          <button
                            className="ghost"
                            onClick={() => {
                              // Список записей несёт только флаг `has_session`: сессию ищем
                              // по окну, а если её нет в выдаче — start вернёт существующую.
                              const s = sessions.find((x) => x.appointment_id === a.id);
                              if (s) setSession(s);
                              else start(a);
                            }}
                          >
                            Открыть приём
                          </button>
                        ) : (
                          active && (
                            <button type="button" onClick={() => start(a)}>
                              Начать приём
                            </button>
                          )
                        )}
                        {active && (
                          <>
                            <button className="ghost" onClick={() => openEdit(a)}>
                              Изменить
                            </button>
                            <button className="ghost" onClick={() => cancel(a)}>
                              Отменить
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>,
                  editing === a.id ? (
                    <tr key={`${a.id}-edit`}>
                      <td colSpan={6}>
                        <div className="row">
                          <label>
                            Новое время
                            <input
                              type="time"
                              value={editTime}
                              onChange={(e) => setEditTime(e.target.value)}
                            />
                          </label>
                          <label>
                            Новая длительность
                            <select
                              value={editDuration}
                              onChange={(e) => setEditDuration(Number(e.target.value))}
                            >
                              {DURATIONS.map((d) => (
                                <option key={d} value={d}>
                                  {d} мин
                                </option>
                              ))}
                            </select>
                          </label>
                          <label>
                            Новый канал
                            <select
                              value={editChannel}
                              onChange={(e) => setEditChannel(e.target.value as AppointmentChannel)}
                            >
                              {CHANNELS.map((c) => (
                                <option key={c} value={c}>
                                  {CHANNEL_LABELS[c]}
                                </option>
                              ))}
                            </select>
                          </label>
                          <label>
                            Новый кабинет
                            <input
                              value={editLocation}
                              maxLength={200}
                              onChange={(e) => setEditLocation(e.target.value)}
                            />
                          </label>
                          <label>
                            Новый статус
                            <select
                              value={editStatus}
                              onChange={(e) => setEditStatus(e.target.value as AppointmentStatus)}
                            >
                              {(Object.keys(STATUS_LABELS) as AppointmentStatus[]).map((s) => (
                                <option key={s} value={s}>
                                  {STATUS_LABELS[s]}
                                </option>
                              ))}
                            </select>
                          </label>
                          <button type="button" onClick={() => saveEdit(a)}>
                            Сохранить запись
                          </button>
                          <button className="ghost" onClick={() => setEditing(null)}>
                            Отмена
                          </button>
                        </div>
                      </td>
                    </tr>
                  ) : null,
                ];
              })}
            </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <h2>Новый приём</h2>
        {rosterError && (
          <p className="error" role="alert">
            Не удалось загрузить пациентов: {rosterError}
          </p>
        )}
        <div className="row">
          <label>
            Пациент
            <select value={patientId} onChange={(e) => setPatientId(e.target.value)}>
              {patients.map((c) => (
                <option key={c.patient_id} value={c.patient_id}>
                  {c.fio ?? c.email}
                </option>
              ))}
            </select>
          </label>
          <label>
            Дата
            <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          </label>
          <label>
            Время
            <input type="time" value={time} onChange={(e) => setTime(e.target.value)} />
          </label>
          <label>
            Длительность
            <select value={duration} onChange={(e) => setDuration(Number(e.target.value))}>
              {DURATIONS.map((d) => (
                <option key={d} value={d}>
                  {d} мин
                </option>
              ))}
            </select>
          </label>
          <label>
            Канал
            <select
              value={channel}
              onChange={(e) => setChannel(e.target.value as AppointmentChannel)}
            >
              {CHANNELS.map((c) => (
                <option key={c} value={c}>
                  {CHANNEL_LABELS[c]}
                </option>
              ))}
            </select>
          </label>
          <label>
            Кабинет
            <input
              value={location}
              maxLength={200}
              placeholder="Каб. 12"
              onChange={(e) => setLocation(e.target.value)}
            />
          </label>
        </div>
        {formErr && (
          <p className="error" role="alert">
            {formErr}
          </p>
        )}
        {formSaved && !formErr && <p className="small muted">Запись создана.</p>}
        <div className="row">
          <button type="button" onClick={create} disabled={busy || !patients.length}>
            {busy ? "Сохраняем…" : "Записать"}
          </button>
          <button type="button" className="ghost" onClick={() => setWalkIn((v) => !v)}>
            Начать приём без записи
          </button>
        </div>
        {walkIn && (
          <div className="row" style={{ marginTop: 10 }}>
            <label>
              Пациент без записи
              <select value={walkPatient} onChange={(e) => setWalkPatient(e.target.value)}>
                <option value="">— выберите —</option>
                {patients.map((c) => (
                  <option key={c.patient_id} value={c.patient_id}>
                    {c.fio ?? c.email}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Канал приёма
              <select
                value={walkChannel}
                onChange={(e) => setWalkChannel(e.target.value as AppointmentChannel)}
              >
                {CHANNELS.map((c) => (
                  <option key={c} value={c}>
                    {CHANNEL_LABELS[c]}
                  </option>
                ))}
              </select>
            </label>
            <button type="button" onClick={startWalkIn}>
              Начать приём
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
