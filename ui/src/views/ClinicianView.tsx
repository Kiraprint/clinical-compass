import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import {
  api,
  AdverseSeverity,
  ApiError,
  Assessment,
  ClinicianAdverseEvent,
  ClinicianProfile,
  ClinicianReminder,
  Drug,
  Escalation,
  HistoryType,
  ImportHistoryResult,
  PatientCard,
  PatientChat,
  PatientContacts,
  PatientHistoryItem,
  PatientMedication,
  RemindersResponse,
  Report,
  Visit,
} from "../api";
import TrendChart from "../TrendChart";
import ChatsView from "./ChatsView";
import DrugLibraryTab from "./DrugLibraryTab";

// Текст ошибки для пользователя: ApiError несёт серверный detail.
const errText = (e: unknown): string =>
  e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e);

// Ошибка загрузки — это не «пустой список»: 500 от API не должен выглядеть как
// «пациентов нет». «Повторить» перезапрашивает те же данные без F5.
function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="alert-danger" role="alert">
      <b>Не удалось загрузить данные.</b> <span className="small">{message}</span>
      <div style={{ marginTop: 6 }}>
        <button className="ghost" onClick={onRetry}>
          Повторить
        </button>
      </div>
    </div>
  );
}

const MED_STATUS_LABELS: Record<string, string> = {
  active: "активно",
  completed: "завершено",
  stopped: "отменено",
};

// FR-27: человекочитаемые подписи отметок приёма и уточнений «частично».
const ADH_STATUS_LABELS: Record<string, string> = {
  taken: "Принял",
  partial: "Частично",
  missed: "Не принял",
  not_scheduled: "Не назначено сегодня",
};
const ADH_PARTIAL_LABELS: Record<string, string> = {
  quarter: "1/4 таблетки",
  half: "1/2 таблетки",
  three_quarters: "3/4 таблетки",
  one_of_two: "1 из 2 доз",
  two_of_three: "2 из 3 доз",
  less_than_prescribed: "Принял меньше, чем назначено",
  self_changed: "Изменил дозу сам",
};

// FR-31: подписи видов фактов из переписки (в карточку попадают только accepted).
const INSIGHT_KIND_LABELS: Record<string, string> = {
  symptom: "Симптом",
  medication: "Препарат",
  adherence: "Приверженность",
  sleep: "Сон",
  substance: "ПАВ",
  risk: "Риск",
  other: "Другое",
};

// FR-32: видимые подписи врачебного интерфейса. Слово «триаж» в UI не
// используется — только «риск» (внутренние поля API остаются triage).
const RISK_LABELS: Record<string, string> = {
  RED: "высокий",
  YELLOW: "средний",
  GREEN: "низкий",
};

const ROLE_LABELS: Record<string, string> = {
  PATIENT: "Пациент",
  CLINICIAN: "Врач",
  ADMIN: "Администратор",
};

// FR-32: подсветка совпавшей части в подсказке препарата (autocomplete).
function Highlight({ text, query }: { text: string; query: string }) {
  const at = query ? text.toLowerCase().indexOf(query.toLowerCase()) : -1;
  if (at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <mark>{text.slice(at, at + query.length)}</mark>
      {text.slice(at + query.length)}
    </>
  );
}

// Сворачиваемый блок карточки врача: состояние раскрытия запоминаем в localStorage,
// чтобы врач не терял привычный вид между визитами (отзыв эксперта: шкала Гамильтона
// занимает весь экран).
function Collapsible({
  storageKey,
  summary,
  children,
  defaultOpen = true,
}: {
  storageKey: string;
  summary: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(() => {
    try {
      const saved = localStorage.getItem(storageKey);
      return saved === null ? defaultOpen : saved !== "0";
    } catch {
      return defaultOpen;
    }
  });
  return (
    <details
      open={open}
      onToggle={(e) => {
        const next = e.currentTarget.open;
        setOpen(next);
        try {
          localStorage.setItem(storageKey, next ? "1" : "0");
        } catch {
          // приватный режим — просто не запоминаем
        }
      }}
    >
      <summary className="collapse-summary">{summary}</summary>
      {children}
    </details>
  );
}

const TRIAGE_FILTERS = ["", "GREEN", "YELLOW", "RED"] as const;

const ESCALATION_KINDS: Record<string, string> = {
  CHECKIN_RISK: "чек-ин: риск",
  CHECKIN_RISK_URGENT: "чек-ин: срочный сигнал",
  SUICIDAL: "скрининг: суицидальный риск",
  SEVERE: "скрининг: тяжёлое состояние",
};

// Пункты риска, которые сервер кладёт в risk_answers (только отвеченные, 0/1).
const RISK_ITEM_IDS = ["HR4", "RS1", "RS2", "RS3"] as const;

// Общая очередь открытых эскалаций (docs/16 §3): сигнал виден даже без roster-врача.
// Карточку открываем только для своего пациента (in_roster): иначе сервер отдаст 403.
function EscalationsQueue({
  items,
  onResolved,
}: {
  items: Escalation[];
  onResolved: (id: string) => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");
  // История закрытых сигналов (status=resolved) — только чтение, по запросу.
  const [showClosed, setShowClosed] = useState(false);
  const [closed, setClosed] = useState<Escalation[]>([]);
  const [closedError, setClosedError] = useState("");
  const [closedLoading, setClosedLoading] = useState(false);
  useEffect(() => {
    if (!showClosed) return;
    let cancelled = false;
    setClosedLoading(true);
    setClosedError("");
    api
      .openEscalations("resolved")
      .then((resp) => {
        if (!cancelled) setClosed(resp.items);
      })
      .catch(() => {
        if (!cancelled) {
          setClosed([]);
          setClosedError("Не удалось загрузить закрытые сигналы");
        }
      })
      .finally(() => {
        if (!cancelled) setClosedLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [showClosed]);
  const closeSignal = async (id: string) => {
    setBusyId(id);
    setError("");
    try {
      await api.resolveEscalation(id);
      onResolved(id);
    } catch (err) {
      // Уже закрыт другим врачом или строки больше нет — из очереди её всё равно убираем.
      if (err instanceof ApiError && (err.code === "ESCALATION_ALREADY_RESOLVED" || err.status === 404)) {
        onResolved(id);
      } else {
        setError(err instanceof ApiError ? err.message : "Не удалось закрыть сигнал");
      }
    } finally {
      setBusyId(null);
    }
  };
  // Блок виден всегда: даже без открытых сигналов он даёт доступ к истории разбора.
  return (
    <div className="card">
      <Collapsible
        storageKey="cc.clinician.escalationsOpen"
        summary={<>Очередь эскалаций ({items.length})</>}
      >
        <p className="muted small">
          Зачем это нужно: здесь сигналы, требующие вашего разбора, — риск по чек-ину,
          RED-риск скрининга и тяжёлые нежелательные явления; сигналы видны даже по
          пациентам без закреплённого врача.
        </p>
        <p style={{ marginBottom: 12 }}>
          <button aria-pressed={showClosed} onClick={() => setShowClosed((v) => !v)}>
            {showClosed ? "Только открытые" : "Показать закрытые"}
          </button>
        </p>
        {error && <p style={{ color: "#b3261e", fontSize: 13 }}>{error}</p>}
        {showClosed ? (
          <>
            {closedError && <p style={{ color: "#b3261e", fontSize: 13 }}>{closedError}</p>}
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Закрыт</th>
                    <th>Пациент</th>
                    <th>Тип</th>
                    <th>Риск</th>
                    <th>Комментарий</th>
                  </tr>
                </thead>
                <tbody>
                  {closedLoading ? (
                    <tr>
                      <td colSpan={5}>Загрузка…</td>
                    </tr>
                  ) : closed.length === 0 && !closedError ? (
                    <tr>
                      <td colSpan={5}>Закрытых сигналов нет</td>
                    </tr>
                  ) : (
                    closed.map((e) => (
                      <tr key={e.id} data-escalation={e.id}>
                        <td>{e.resolved_at ? new Date(e.resolved_at).toLocaleString("ru-RU") : "—"}</td>
                        <td>
                          {e.in_roster ? (
                            <a
                              href={"#patient=" + e.patient_id}
                              style={{ color: "#2f6fed", textDecoration: "none", fontWeight: 600 }}
                            >
                              {e.patient_fio ?? "Пациент"}
                            </a>
                          ) : (
                            <span style={{ fontWeight: 600 }}>{e.patient_fio ?? "Пациент"}</span>
                          )}
                        </td>
                        <td>
                          <span
                            className={`badge ${
                              e.kind === "CHECKIN_RISK_URGENT" || e.kind === "SUICIDAL" ? "RED" : "YELLOW"
                            }`}
                          >
                            {ESCALATION_KINDS[e.kind] ?? e.kind}
                          </span>
                        </td>
                        <td>{e.safety ?? "—"}</td>
                        <td style={{ fontSize: 13 }}>{e.resolution?.trim() ? e.resolution : "без комментария"}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Дата</th>
                  <th>Пациент</th>
                  <th>Тип</th>
                  <th>Риск</th>
                  <th>Ответы риска</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {items.length === 0 && (
                  <tr>
                    <td colSpan={6}>Открытых сигналов нет</td>
                  </tr>
                )}
                {items.map((e) => (
                  <tr key={e.id} data-escalation={e.id}>
                    <td>{e.created_at ? new Date(e.created_at).toLocaleString("ru-RU") : "—"}</td>
                    <td>
                      {e.in_roster ? (
                        <a
                          href={"#patient=" + e.patient_id}
                          style={{ color: "#2f6fed", textDecoration: "none", fontWeight: 600 }}
                        >
                          {e.patient_fio ?? "Пациент"}
                        </a>
                      ) : (
                        <span style={{ fontWeight: 600 }}>{e.patient_fio ?? "Пациент"}</span>
                      )}
                    </td>
                    <td>
                      <span
                        className={`badge ${
                          e.kind === "CHECKIN_RISK_URGENT" || e.kind === "SUICIDAL" ? "RED" : "YELLOW"
                        }`}
                      >
                        {ESCALATION_KINDS[e.kind] ?? e.kind}
                      </span>
                    </td>
                    <td>{e.safety ?? "—"}</td>
                    <td style={{ fontSize: 13 }}>
                      {RISK_ITEM_IDS.filter((id) => e.risk_answers?.[id] != null)
                        .map((id) => `${id} ${e.risk_answers?.[id] ? "Да" : "Нет"}`)
                        .join(" · ") || "—"}
                    </td>
                    <td>
                      <button
                        onClick={() => closeSignal(e.id)}
                        disabled={busyId === e.id}
                        aria-label={`Закрыть сигнал ${e.patient_fio ?? e.patient_id}`}
                      >
                        {busyId === e.id ? "Закрываем…" : "Закрыть сигнал"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Collapsible>
    </div>
  );
}

// FR-24: напоминания врача. Один компактный список для кабинета и расписания.
const REMINDER_KINDS: { value: ClinicianReminder["kind"]; label: string }[] = [
  { value: "call", label: "Позвонить" },
  { value: "write", label: "Написать" },
  { value: "appointment", label: "Назначить приём" },
  { value: "custom", label: "Другое" },
];

// Порядок групп в панели: сначала просроченное, затем сегодня/скоро/позже.
const REMINDER_GROUPS: { key: NonNullable<ClinicianReminder["due_state"]>; label: string }[] = [
  { key: "overdue", label: "Просрочено" },
  { key: "today", label: "Сегодня" },
  { key: "soon", label: "Ближайшие 7 дней" },
  { key: "later", label: "Позже" },
];
const REMINDER_STATE_LABELS: Record<string, string> = {
  overdue: "просрочено",
  today: "сегодня",
  soon: "скоро",
  later: "позже",
};

function nextHourLocal(): string {
  const d = new Date(Date.now() + 3600_000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtDue(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// FR-24: список напоминаний с действиями. Компактный вид — для кабинета,
// полный (с заметкой и отменой) — для расписания.
function ReminderList({
  items,
  compact = false,
  onChanged,
  onOpenPatient,
}: {
  items: ClinicianReminder[];
  compact?: boolean;
  onChanged: () => void;
  onOpenPatient?: (patientId: string) => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");

  const act = async (id: string, fn: (id: string) => Promise<unknown>) => {
    setBusyId(id);
    setError("");
    try {
      await fn(id);
      onChanged();
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const snooze = (r: ClinicianReminder) => {
    const next = new Date(r.due_at);
    next.setDate(next.getDate() + 1);
    return act(r.id, () => api.patchReminder(r.id, { due_at: next.toISOString() }));
  };

  if (items.length === 0) {
    return <p className="muted small">Открытых напоминаний нет.</p>;
  }
  return (
    <div>
      {error && <p className="error">{error}</p>}
      <ul className="reminder-list">
        {items.map((r) => (
          <li key={r.id} className={`reminder-item ${r.due_state ?? ""}`}>
            <div className="reminder-main">
              <strong>{r.kind_label}</strong>
              {" · "}
              {onOpenPatient ? (
                <button
                  type="button"
                  className="link-patient"
                  onClick={() => onOpenPatient(r.patient_id)}
                >
                  {r.patient_fio}
                </button>
              ) : (
                <span>{r.patient_fio}</span>
              )}
              <span className="muted small"> · срок {fmtDue(r.due_at)}</span>
              {r.due_state && <span className="reminder-state">{REMINDER_STATE_LABELS[r.due_state]}</span>}
              {!compact && r.note && <div className="reminder-note">{r.note}</div>}
            </div>
            <div className="reminder-actions">
              <button
                className="ghost"
                disabled={busyId === r.id}
                onClick={() => void act(r.id, api.doneReminder)}
              >
                Выполнено
              </button>
              <button className="ghost" disabled={busyId === r.id} onClick={() => void snooze(r)}>
                +1 день
              </button>
              {!compact && (
                <button
                  className="ghost"
                  disabled={busyId === r.id}
                  onClick={() => void act(r.id, () => api.cancelReminder(r.id))}
                >
                  Отменить
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function DoctorProfileCard({ profile }: { profile: ClinicianProfile | null }) {
  return (
    <div className="card">
      <h2>Личный кабинет</h2>
      {profile ? (
        <>
          <p style={{ margin: "2px 0" }}>
            <b>{profile.fio ?? "ФИО не указано"}</b>
          </p>
          <p className="muted small" style={{ margin: "2px 0" }}>
            {profile.email}
          </p>
          <p className="muted small" style={{ margin: "2px 0" }}>
            Роли: {profile.roles.map((r) => ROLE_LABELS[r] ?? r).join(", ")}
          </p>
        </>
      ) : (
        <p className="muted small">Загрузка профиля…</p>
      )}
      <p className="profile-links">
        <a href="/legal/terms">Пользовательское соглашение</a>
        <a href="/legal/consent">Согласие на обработку персональных данных</a>
      </p>
    </div>
  );
}

function DashboardChats({
  chats,
  onOpenChats,
}: {
  chats: PatientChat[];
  onOpenChats: (patientId: string) => void;
}) {
  return (
    <div className="card">
      <h2>Чаты с пациентами</h2>
      {chats.length === 0 ? (
        <p className="muted small">Сообщений от пациентов пока нет.</p>
      ) : (
        <ul className="chat-list-items dash-chat-list">
          {chats.slice(0, 6).map((c) => (
            <li key={c.patient_id}>
              <button className="chat-item" onClick={() => onOpenChats(c.patient_id)}>
                <span className="chat-item-title">{c.fio ?? "Пациент"}</span>
                <span className="preview">{c.preview}</span>
                <span className="meta">
                  {fmtDue(c.last_at)}
                  {c.last_sender === "PATIENT" ? " · ждёт ответа" : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <button className="ghost" onClick={() => onOpenChats("")}>
        Все чаты
      </button>
    </div>
  );
}

function Dashboard({
  fio,
  cards,
  escalations,
  pendingAe,
  reminders,
  chats,
  profile,
  cardsError,
  escalationsError,
  remindersError,
  onResolved,
  onChanged,
  onOpenChats,
  onRetry,
}: {
  fio: string | null;
  cards: PatientCard[];
  escalations: Escalation[];
  pendingAe: ClinicianAdverseEvent[];
  reminders: ClinicianReminder[];
  chats: PatientChat[];
  profile: ClinicianProfile | null;
  cardsError: string;
  escalationsError: string;
  remindersError: string;
  onResolved: (id: string) => void;
  onChanged: () => void;
  onOpenChats: (patientId: string) => void;
  onRetry: () => void;
}) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 30000);
    return () => window.clearInterval(t);
  }, []);

  const hour = now.getHours();
  const greeting =
    hour < 6 ? "Доброй ночи" : hour < 12 ? "Доброе утро" : hour < 18 ? "Добрый день" : "Добрый вечер";
  const worse = cards
    .filter((c) => c.last_triage === "RED" || c.open_escalations > 0)
    .sort((a, b) => b.open_escalations - a.open_escalations);
  const overdue = reminders.filter((r) => r.due_state === "overdue").length;
  // Если список не загрузился, «ухудшений нет» — ложное утверждение.
  const loadFailed = Boolean(cardsError || escalationsError);
  const openPatient = (patientId: string) => {
    window.location.hash = "patient=" + patientId;
  };

  return (
    <div className="dash">
      <div className="dash-main">
        <div className="card dash-hello">
          <h2>
            {greeting}
            {fio ? `, ${fio}` : ""}
          </h2>
          <p className="muted">
            {now.toLocaleDateString("ru-RU", {
              weekday: "long",
              day: "numeric",
              month: "long",
              year: "numeric",
            })}
            {" · "}
            {now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}
          </p>
          <p className="muted small">
            Пациентов в наблюдении: {cardsError ? "—" : cards.length} · открытых сигналов:{" "}
            {escalationsError ? "—" : escalations.length} · напоминаний:{" "}
            {remindersError ? "—" : reminders.length}
            {pendingAe.length > 0 ? ` · НЯ на проверке: ${pendingAe.length}` : ""}
          </p>
        </div>

        {loadFailed && (
          <LoadError
            message={[cardsError, escalationsError].filter(Boolean).join(" · ")}
            onRetry={onRetry}
          />
        )}

        {worse.length > 0 ? (
          <div className="alert-danger">
            <b>Пациенты, которым стало хуже ({worse.length})</b>
            <ul className="dash-alert-list">
              {worse.slice(0, 5).map((c) => (
                <li key={c.patient_id}>
                  <a href={"#patient=" + c.patient_id}>{c.fio ?? c.email}</a>{" "}
                  {c.last_triage && <span className={`badge ${c.last_triage}`}>{c.last_triage}</span>}{" "}
                  {c.open_escalations > 0 && (
                    <span className="muted small">открытых сигналов: {c.open_escalations}</span>
                  )}
                </li>
              ))}
            </ul>
            {worse.length > 5 && <p className="small">…и ещё {worse.length - 5}.</p>}
            <p className="small">
              Красный риск скрининга или открытые эскалации — разберите в очереди ниже.
            </p>
          </div>
        ) : loadFailed ? null : (
          <div className="alert-info">
            Ухудшений нет: красных рисков и открытых сигналов не зафиксировано.
          </div>
        )}

        {pendingAe.length > 0 && (
          <div className="alert-warn">
            <strong>НЯ на проверке: {pendingAe.length}</strong>
            <div className="small">
              {pendingAe.slice(0, 3).map((e) => e.patient_fio ?? "Пациент").join(", ")}
              {pendingAe.length > 3 ? " …" : ""} — откройте карточку пациента.
            </div>
          </div>
        )}

        <EscalationsQueue items={escalations} onResolved={onResolved} />
        <DoctorProfileCard profile={profile} />
      </div>

      <aside className="dash-side" aria-label="Напоминания и чаты">
        <div className="card">
          <h2>
            Напоминания{" "}
            {overdue > 0 && <span className="badge RED">{overdue} просрочено</span>}
          </h2>
          {remindersError ? (
            <LoadError message={remindersError} onRetry={onRetry} />
          ) : (
            <ReminderList items={reminders} compact onChanged={onChanged} onOpenPatient={openPatient} />
          )}
        </div>
        <DashboardChats chats={chats} onOpenChats={onOpenChats} />
      </aside>
    </div>
  );
}

// FR-24/FR-32: «Расписание» — планирование созвонов и приёмов. Использует те же
// напоминания врача (kind=appointment для приёма), отдельного календаря нет.
function ScheduleView({
  cards,
  version,
  onChanged,
}: {
  cards: PatientCard[];
  version: number;
  onChanged: () => void;
}) {
  const [items, setItems] = useState<ClinicianReminder[]>([]);
  const [patientId, setPatientId] = useState("");
  const [kind, setKind] = useState<ClinicianReminder["kind"]>("appointment");
  const [due, setDue] = useState(nextHourLocal());
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!patientId && cards.length > 0) setPatientId(cards[0].patient_id);
  }, [cards, patientId]);

  useEffect(() => {
    let cancelled = false;
    api
      .clinicianReminders()
      .then((r) => {
        if (!cancelled) setItems(r.items);
      })
      .catch((e: unknown) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [version]);

  const create = async () => {
    if (!patientId || !due) {
      setErr("Выберите пациента и срок");
      return;
    }
    setBusy(true);
    setErr("");
    setSaved(false);
    try {
      await api.createReminder({
        patient_id: patientId,
        kind,
        note: note.trim() || null,
        due_at: new Date(due).toISOString(),
      });
      setNote("");
      setSaved(true);
      onChanged();
    } catch (e: unknown) {
      setErr(e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <h2>Расписание</h2>
      <p className="muted small">
        Планируйте созвоны и приёмы: напоминание появится в кабинете и в списке ниже.
        «Назначить приём» — это запись пациента на приём.
      </p>
      <div className="row" style={{ alignItems: "flex-end", gap: 8, flexWrap: "wrap" }}>
        <label style={{ minWidth: 200 }}>
          Пациент
          <select value={patientId} onChange={(e) => setPatientId(e.target.value)}>
            {cards.map((c) => (
              <option key={c.patient_id} value={c.patient_id}>
                {c.fio ?? c.email}
              </option>
            ))}
          </select>
        </label>
        <label>
          Вид
          <select value={kind} onChange={(e) => setKind(e.target.value as ClinicianReminder["kind"])}>
            {REMINDER_KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {k.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Срок
          <input type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} />
        </label>
        <label style={{ flex: 1, minWidth: 220 }}>
          Заметка (необязательно, шифруется)
          <input value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} />
        </label>
        <button onClick={() => void create()} disabled={busy || !patientId || !due}>
          Запланировать
        </button>
      </div>
      {saved && <p style={{ color: "#2e7d32", fontSize: 13 }}>Добавлено в расписание</p>}
      {err && <p className="error">{err}</p>}

      {items.length === 0 ? (
        <p className="muted small">Открытых напоминаний нет.</p>
      ) : (
        <div className="schedule-groups">
          {REMINDER_GROUPS.map((g) => {
            const group = items.filter((r) => (r.due_state ?? "later") === g.key);
            if (group.length === 0) return null;
            return (
              <div key={g.key} className="schedule-group">
                <h3>
                  {g.label} ({group.length})
                </h3>
                <ReminderList items={group} onChanged={onChanged} />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

type Section = "dashboard" | "patients" | "assistant" | "chats" | "schedule" | "library";

const NAV_ITEMS: { id: Section; label: string }[] = [
  { id: "dashboard", label: "Кабинет" },
  { id: "patients", label: "Пациенты" },
  { id: "assistant", label: "Ассистент" },
  { id: "chats", label: "Чаты с пациентами" },
  { id: "schedule", label: "Расписание" },
  { id: "library", label: "Библиотека" },
];

export default function ClinicianView() {
  // FR-32: разделы кабинета. Меню слева под «три полоски»; состояние храним,
  // чтобы врач не открывал его каждый раз.
  const [section, setSection] = useState<Section>("dashboard");
  const [navOpen, setNavOpen] = useState(() => {
    try {
      return localStorage.getItem("cc.clinician.navOpen") === "1";
    } catch {
      return false;
    }
  });
  // FR-29: выбранный пациент на экране «Чаты» (deep-link #chats=patient:<id>).
  const [chatPatient, setChatPatient] = useState<string | null>(null);
  const [filter, setFilter] = useState<(typeof TRIAGE_FILTERS)[number]>("");
  const [cards, setCards] = useState<PatientCard[]>([]);
  // Ошибки загрузок показываем явно: пустой список из-за 500 — не «нет пациентов».
  const [cardsError, setCardsError] = useState("");
  const [remindersError, setRemindersError] = useState("");
  const [escalationsError, setEscalationsError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [reportError, setReportError] = useState("");
  // Счётчик перезагрузки: врачебная оценка меняет риск/эскалации, и карточка
  // со списком должны показать это сразу, без ручного F5.
  const [reportVersion, setReportVersion] = useState(0);
  const [reminderVersion, setReminderVersion] = useState(0);
  const [reminders, setReminders] = useState<ClinicianReminder[]>([]);
  const [profile, setProfile] = useState<ClinicianProfile | null>(null);
  const [chats, setChats] = useState<PatientChat[]>([]);
  // Общая очередь эскалаций — не только свои пациенты (docs/16 §3).
  const [escalations, setEscalations] = useState<Escalation[]>([]);
  // FR-28: НЯ, сообщённые пациентами и ждущие проверки врача.
  const [pendingAe, setPendingAe] = useState<ClinicianAdverseEvent[]>([]);

  const toggleNav = () => {
    setNavOpen((v) => {
      const next = !v;
      try {
        localStorage.setItem("cc.clinician.navOpen", next ? "1" : "0");
      } catch {
        // приватный режим — не запоминаем
      }
      return next;
    });
  };

  const openSection = (next: Section) => {
    setSection(next);
    if (next !== "patients") setSelected(null);
    if (window.innerWidth <= 900) setNavOpen(false);
  };

  useEffect(() => {
    let cancelled = false;
    api
      .clinicianMe()
      .then((p) => {
        if (!cancelled) setProfile(p);
      })
      .catch(() => {
        if (!cancelled) setProfile(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .clinicianReminders()
      .then((r) => {
        if (!cancelled) {
          setReminders(r.items);
          setRemindersError("");
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setRemindersError(errText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [reminderVersion, reportVersion]);

  useEffect(() => {
    let cancelled = false;
    api
      .clinicianPatients(filter || undefined)
      .then((data) => {
        if (!cancelled) {
          setCards(data.items);
          setCardsError("");
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setCardsError(errText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [filter, reportVersion]);

  useEffect(() => {
    if (section !== "dashboard") return;
    let cancelled = false;
    api
      .openEscalations()
      .then((r) => {
        if (!cancelled) {
          setEscalations(r.items);
          setEscalationsError("");
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setEscalationsError(errText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [section, reportVersion]);

  useEffect(() => {
    if (section !== "dashboard") return;
    let cancelled = false;
    api
      .adverseQueue("pending")
      .then((r) => {
        if (!cancelled) setPendingAe(r.items);
      })
      .catch(() => {
        if (!cancelled) setPendingAe([]);
      });
    return () => {
      cancelled = true;
    };
  }, [section, reportVersion]);

  useEffect(() => {
    if (section !== "dashboard" && section !== "chats") return;
    let cancelled = false;
    api
      .clinicianChats()
      .then((r) => {
        if (!cancelled) setChats(r.items);
      })
      .catch(() => {
        if (!cancelled) setChats([]);
      });
    return () => {
      cancelled = true;
    };
  }, [section, reportVersion]);

  // Карточка пациента — отдельная ссылка (#patient=<id>): синхронизируем с hash,
  // чтобы клик по уведомлению об эскалации открывал нужного пациента.
  useEffect(() => {
    const applyHash = () => {
      // FR-29: #chats=patient:<id> — прямой переход к переписке с пациентом.
      const chat = window.location.hash.match(/chats=patient:([0-9a-fA-F-]+)/);
      if (chat) {
        setSection("chats");
        setChatPatient((cur) => (cur === chat[1] ? cur : chat[1]));
        setSelected(null);
        return;
      }
      const m = window.location.hash.match(/patient=([0-9a-fA-F-]+)/);
      const pid = m ? m[1] : null;
      if (pid) setSection("patients");
      setSelected((cur) => (cur === pid ? cur : pid));
    };
    applyHash();
    window.addEventListener("hashchange", applyHash);
    return () => window.removeEventListener("hashchange", applyHash);
  }, []);

  // Загрузка карточки при смене выбранного пациента.
  useEffect(() => {
    if (!selected) {
      setReport(null);
      return;
    }
    let cancelled = false;
    setReportError("");
    api
      .clinicianReport(selected)
      .then((r) => {
        if (!cancelled) setReport(r);
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setReport(null);
          setReportError(e instanceof Error ? e.message : String(e));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selected, reportVersion]);

  const openPatient = (patientId: string) => {
    window.location.hash = "patient=" + patientId;
  };

  const openChats = (patientId?: string | null) => {
    openSection("chats");
    setChatPatient(patientId || null);
    if (patientId) window.location.hash = `chats=patient:${patientId}`;
  };

  const bump = () => setReminderVersion((v) => v + 1);
  // Повтор после ошибки: те же загрузчики висят на reportVersion.
  const retryLoads = () => setReportVersion((v) => v + 1);

  const nav = (
    <>
      <button
        className="nav-toggle"
        aria-label="Меню"
        aria-expanded={navOpen}
        aria-controls="clinician-nav"
        onClick={toggleNav}
      >
        <span aria-hidden="true">☰</span> Меню
      </button>
      {navOpen && (
        <div className="nav-backdrop" aria-hidden="true" onClick={() => setNavOpen(false)} />
      )}
      <nav
        id="clinician-nav"
        className={`clinician-nav${navOpen ? " open" : ""}`}
        aria-label="Разделы кабинета врача"
        aria-hidden={!navOpen}
      >
        <div className="nav-head">
          <span>Кабинет врача</span>
          <button
            className="icon-btn ghost nav-close"
            aria-label="Скрыть меню"
            onClick={() => setNavOpen(false)}
          >
            ✕
          </button>
        </div>
        {NAV_ITEMS.map((item) => (
          <button
            key={item.id}
            className={`nav-item${section === item.id ? " active" : ""}`}
            aria-current={section === item.id ? "page" : undefined}
            onClick={() => openSection(item.id)}
          >
            {item.label}
            {item.id === "schedule" &&
              reminders.filter((r) => r.due_state === "overdue").length > 0 && (
                <span className="count-badge">
                  {reminders.filter((r) => r.due_state === "overdue").length}
                </span>
              )}
          </button>
        ))}
      </nav>
    </>
  );

  const content = (() => {
    if (section === "assistant") {
      return (
        <ChatsView
          mode="assistant"
          onSelectPatient={(id) => setChatPatient(id)}
          onModeChange={(m) => setSection(m)}
        />
      );
    }
    if (section === "chats") {
      return (
        <ChatsView
          mode="patients"
          patientId={chatPatient}
          onSelectPatient={(id) => {
            setChatPatient(id);
            if (id) window.location.hash = `chats=patient:${id}`;
          }}
          onModeChange={(m) => setSection(m)}
        />
      );
    }
    if (section === "library") {
      return <DrugLibraryTab />;
    }
    if (section === "schedule") {
      return <ScheduleView cards={cards} version={reminderVersion} onChanged={bump} />;
    }
    if (section === "dashboard") {
      return (
        <Dashboard
          fio={profile?.fio ?? null}
          cards={cards}
          escalations={escalations}
          pendingAe={pendingAe}
          reminders={reminders}
          chats={chats}
          profile={profile}
          cardsError={cardsError}
          escalationsError={escalationsError}
          remindersError={remindersError}
          onResolved={(id) => setEscalations((prev) => prev.filter((e) => e.id !== id))}
          onChanged={bump}
          onOpenChats={(pid) => openChats(pid || null)}
          onRetry={retryLoads}
        />
      );
    }
    if (selected) {
      return (
        <ReportView
          report={report}
          error={reportError}
          onBack={() => {
            window.location.hash = "";
          }}
          onRefresh={() => setReportVersion((v) => v + 1)}
          onOpenLibrary={() => openSection("library")}
          onOpenChats={(pid) => openChats(pid)}
        />
      );
    }
    return (
      <>
        <div className="tabs">
          {TRIAGE_FILTERS.map((f) => (
            <button
              key={f}
              className={filter === f ? "active" : ""}
              aria-pressed={filter === f}
              onClick={() => setFilter(f)}
            >
              {f === "" ? "Все" : f}
            </button>
          ))}
        </div>
        <p className="muted small">
          Риск: GREEN — низкий, YELLOW — средний, RED — высокий. Сначала красные: выше —
          больше открытых эскалаций и более свежая оценка.
        </p>
        {pendingAe.length > 0 && (
          <div className="alert-warn">
            <strong>НЯ на проверке: {pendingAe.length}</strong>
            <div className="small">
              {pendingAe.slice(0, 3).map((e) => e.patient_fio ?? "Пациент").join(", ")}
              {pendingAe.length > 3 ? " …" : ""} — откройте карточку пациента.
            </div>
          </div>
        )}
        <div className="card">
          <h2>Мои пациенты{cardsError ? "" : ` (${cards.length})`}</h2>
          {cardsError ? (
            <LoadError message={cardsError} onRetry={retryLoads} />
          ) : (
            cards.length === 0 && <p className="muted">Нет пациентов в выбранном фильтре.</p>
          )}
          {!cardsError && (
            <div className="table-wrap">
              <table className="patients">
                <thead>
                  <tr>
                    <th>Пациент</th>
                    <th>Риск</th>
                    <th>Балл</th>
                    <th>Эскалации</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {cards.map((c) => (
                    <tr key={c.patient_id}>
                      <td>
                        <a
                          href={"#patient=" + c.patient_id}
                          style={{ color: "#2f6fed", textDecoration: "none", fontWeight: 600 }}
                        >
                          {c.fio ?? c.email}
                        </a>
                        <br />
                        <small>{c.email}</small>
                        {c.phone && (
                          <>
                            <br />
                            <small title="Телефон скрыт частично (152-ФЗ)">{c.phone}</small>
                          </>
                        )}
                      </td>
                      <td>
                        {c.last_triage ? (
                          <span className={`badge ${c.last_triage}`}>{c.last_triage}</span>
                        ) : (
                          <span className="badge GREEN">без наблюдений</span>
                        )}
                      </td>
                      <td>{c.last_score ?? "—"}</td>
                      <td>{c.open_escalations}</td>
                      <td>
                        <button className="ghost" onClick={() => openPatient(c.patient_id)}>
                          Открыть
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </>
    );
  })();

  return (
    <div className={`clinician-app${navOpen ? " nav-open" : ""}`}>
      {nav}
      <div className="clinician-content">{content}</div>
    </div>
  );
}

function ReportView({ report, error, onBack, onRefresh, onOpenLibrary, onOpenChats }: {
  report: Report | null; error: string; onBack: () => void; onRefresh: () => void;
  onOpenLibrary: () => void; onOpenChats: (patientId: string) => void;
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const [scaleSaved, setScaleSaved] = useState<number | null>(null);
  const [scaleError, setScaleError] = useState("");
  // in-flight: двойной клик по «Назначить»/«Сохранить оценку» не должен слать
  // второй запрос и создавать дубли.
  const [scaleBusy, setScaleBusy] = useState(false);
  const [assessBusy, setAssessBusy] = useState(false);
  const [aeList, setAeList] = useState<Report["adverse_events"]>([]);
  // FR-28: справочник ГРЛС для связи НЯ с препаратом (доступ врачу).
  const [drugs, setDrugs] = useState<Drug[]>([]);
  const [histText, setHistText] = useState("");
  const [histResult, setHistResult] = useState<ImportHistoryResult | null>(null);
  const [assessCode, setAssessCode] = useState("");
  const [assessAnswers, setAssessAnswers] = useState<Record<string, number>>({});
  const [assessResult, setAssessResult] = useState<Assessment | null>(null);
  const [assessErr, setAssessErr] = useState("");
  const [histErr, setHistErr] = useState("");
  const [visits, setVisits] = useState<Visit[]>([]);
  const [visitDate, setVisitDate] = useState(new Date().toISOString().slice(0, 10));
  const [visitType, setVisitType] = useState<"in_person" | "online" | "phone">("in_person");
  const [visitDuration, setVisitDuration] = useState("");
  const [visitNotes, setVisitNotes] = useState("");
  const [visitErr, setVisitErr] = useState("");
  const [visitSaved, setVisitSaved] = useState<number | null>(null);
  useEffect(() => {
    if (report) { setPicked(report.prescribed_scales ?? []); setAeList(report.adverse_events ?? []); }
    const clinicianScale = report?.available_scales.find((s) => s.audience === "CLINICIAN");
    setAssessCode(clinicianScale?.code ?? "");
    setAssessAnswers({});
    setAssessResult(null);
    setAssessErr("");
  }, [report]);

  useEffect(() => {
    let cancelled = false;
    api.drugs()
      .then((r) => { if (!cancelled) setDrugs(r.items); })
      .catch(() => { if (!cancelled) setDrugs([]); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!report) return;
    let cancelled = false;
    api.clinicianVisits(report.patient_id)
      .then((r) => { if (!cancelled) setVisits(r.items); })
      .catch(() => { if (!cancelled) setVisits([]); });
    return () => { cancelled = true; };
  }, [report]);

  const addVisit = async () => {
    if (!report || !visitNotes.trim()) return;
    setVisitErr("");
    // Number("") === 0, а Number("abc") === NaN → JSON.stringify даёт null и
    // длительность молча теряется. Проверяем явно.
    const durationRaw = visitDuration.trim();
    const duration = durationRaw ? Number(durationRaw) : undefined;
    if (duration !== undefined && (!Number.isFinite(duration) || duration < 0)) {
      setVisitErr("Длительность должна быть неотрицательным числом минут");
      return;
    }
    try {
      const v = await api.createVisit(report.patient_id, {
        visited_at: new Date(visitDate + "T12:00:00").toISOString(),
        visit_type: visitType,
        duration_min: duration,
        notes: visitNotes.trim(),
      });
      setVisits([v, ...visits]);
      setVisitNotes(""); setVisitDuration(""); setVisitSaved(Date.now());
    } catch (e: unknown) {
      setVisitErr(e instanceof Error ? e.message : String(e));
    }
  };

  const removeVisit = async (visitId: string) => {
    if (!report) return;
    setVisitErr("");
    try {
      await api.deleteVisit(report.patient_id, visitId);
      setVisits(visits.filter((v) => v.id !== visitId));
    } catch (e: unknown) {
      setVisitErr(e instanceof Error ? e.message : String(e));
    }
  };

  const saveScale = async () => {
    if (!report || picked.length === 0 || scaleBusy) return;
    setScaleError("");
    setScaleBusy(true);
    try {
      const res = await api.setPrescribedScales(report.patient_id, picked);
      setScaleSaved(Date.now());
      setPicked(res.scales);
    } catch (e: unknown) {
      setScaleError(e instanceof Error ? e.message : String(e));
    } finally {
      setScaleBusy(false);
    }
  };

  const submitAssessment = async () => {
    if (!report || !assessCode || assessBusy) return;
    const scale = report.available_scales.find((s) => s.code === assessCode);
    if (!scale) return;
    setAssessErr("");
    setAssessBusy(true);
    try {
      const answers = Object.fromEntries(
        scale.items.map((i) => [i.key, assessAnswers[i.key] ?? 0]),
      );
      setAssessResult(await api.createClinicianAssessment(report.patient_id, scale.code, answers));
      onRefresh();
    } catch (e: unknown) {
      setAssessErr(e instanceof Error ? e.message : String(e));
    } finally {
      setAssessBusy(false);
    }
  };

  const runImport = async () => {
    if (!report || !histText.trim()) return;
    setHistErr("");
    setHistResult(null);
    try {
      setHistResult(await api.importHistory(report.patient_id, histText.trim()));
    } catch (e: unknown) {
      setHistErr(e instanceof Error ? e.message : String(e));
    }
  };

  const jump = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  if (error) {
    return (
      <div className="card">
        <button className="ghost" onClick={onBack}>
          ← Назад к списку
        </button>
        <p className="alert-danger">Не удалось загрузить отчёт: {error}</p>
      </div>
    );
  }
  if (!report) return <div className="card">Загрузка…</div>;
  const last = report.last_assessment;
  // FR-34: пункт о суициде (q9 PHQ-9, h3 HDRS-17) спрашивается не в каждой шкале.
  // Бэкенд отдаёт пропуски последней шкалы, у которой такой пункт есть, — иначе
  // после GAD-7 предупреждение пропадало бы, хотя PHQ-9 спрашивали без q9.
  const lastAudit = last as { omitted_items?: string[]; omitted_scale?: string | null } | null;
  const omittedItems = lastAudit?.omitted_items ?? [];
  const omittedScale = lastAudit?.omitted_scale ?? null;
  const pendingAeCount = aeList.filter((e) => e.status === "pending").length;
  const assessScale = report.available_scales.find((s) => s.code === assessCode);
  const clinicianScales = report.available_scales.filter((s) => s.audience === "CLINICIAN");
  const patientScales = report.available_scales.filter((s) => s.audience === "PATIENT");
  // Итог в summary свёрнутой карточки: свежая оценка из формы или последняя сохранённая.
  const assessFmt = (score: number, sev: string, at: string) =>
    `${score} · ${sev} · ${new Date(at).toLocaleDateString("ru-RU")}`;
  const assessSummary = assessResult
    ? assessFmt(assessResult.total_score, assessResult.severity, assessResult.computed_at)
    : report.last_assessment
      ? `последняя ${assessFmt(report.last_assessment.total_score, report.last_assessment.severity, report.last_assessment.computed_at)}`
      : "";
  const checkinReviewed = report.checkin.recent.filter((c) => c.safety !== "none");
  const checkinSummary =
    report.checkin.recent.length === 0
      ? "Чек-ин: данных нет"
      : checkinReviewed.length > 0
        ? "Чек-ин: есть риск"
        : "Чек-ин: нет риска";
  const triage = last?.triage ?? null;
  const highRisk = triage === "RED" || report.open_escalations.length > 0;
  const adherencePct = report.adherence.pct;

  return (
    <div className="card">
      <button className="ghost" onClick={onBack}>
        ← Назад к списку
      </button>
      <h2 style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
        {report.fio ?? report.email}
        {report.fio && report.email && (
          <span style={{ color: "#5a6478", fontSize: 14, fontWeight: 400 }}>{report.email}</span>
        )}
      </h2>

      <div className="patient-actions">
        <button className="ghost" onClick={() => jump("clinician-visits")}>
          Приёмы
        </button>
        <button className="ghost" onClick={() => jump("clinician-medications")}>
          Назначения
        </button>
        <button className="ghost" onClick={() => onOpenChats(report.patient_id)}>
          Открыть в чатах
        </button>
      </div>

      <div className="patient-risk">
        <div>
          <span className="muted small">Риск по последней оценке: </span>
          {triage ? (
            <span className={`badge ${triage}`}>
              {triage} — {RISK_LABELS[triage] ?? ""}
            </span>
          ) : (
            <span className="badge GREEN">нет оценки</span>
          )}
          {last && (
            <span className="muted small">
              {" "}
              · балл {last.total_score} · {last.severity} · {last.computed_at.slice(0, 10)}
            </span>
          )}
        </div>
        {omittedItems.length > 0 && (
          <div className="risk-note">
            Пункт {omittedItems.join(", ")} не задавался
            {omittedScale ? ` в шкале ${omittedScale}` : ""} · риск не оценён
          </div>
        )}
        <div style={{ marginTop: 6 }}>
          {report.routing ? (
            <>
              <span className="muted small">Маршрутизация: </span>
              <b>{report.routing.label}</b>
              <span className="muted small"> · {report.routing.target} · {report.routing.reason}</span>
            </>
          ) : (
            <span className="muted small">Маршрутизация: не требуется — динамическое наблюдение</span>
          )}
        </div>
      </div>

      {highRisk && (
        <div className="alert-danger">
          <b>Высокий риск: требуется разбор.</b>{" "}
          {report.open_escalations.length > 0
            ? `Открытых сигналов: ${report.open_escalations.length} — они в «Очереди эскалаций» кабинета.`
            : "Последняя оценка — RED."}
        </div>
      )}

      <ContactsBlock patientId={report.patient_id} hasConsent={report.contacts !== null} />

      <AnamnesisBlock patientId={report.patient_id} initial={report.anamnesis} />

      <Collapsible storageKey="cc.clinician.reminders" summary="Напоминания">
        <PatientReminders patientId={report.patient_id} />
      </Collapsible>

      <Collapsible
        storageKey="cc.clinician.screening-scales"
        summary={
          <>
            Назначить скрининг
            {assessSummary && (
              <span className="muted small" style={{ fontWeight: 400 }}>
                {" "}· оценка врача: {assessSummary}
              </span>
            )}
          </>
        }
      >
        <h4 className="subhead">Шкалы для пациента</h4>
        <table className="scale-table" style={{ fontSize: 14 }}>
          <tbody>
            {patientScales.map((s) => (
              <tr key={s.code}>
                <td style={{ textAlign: "left" }}>
                  <label htmlFor={s.code}>{s.title}</label>
                </td>
                <td style={{ textAlign: "right", width: 44, whiteSpace: "nowrap" }}>
                  <input
                    id={s.code}
                    type="checkbox"
                    checked={picked.includes(s.code)}
                    onChange={() =>
                      setPicked((cur) =>
                        cur.includes(s.code) ? cur.filter((c) => c !== s.code) : [...cur, s.code],
                      )
                    }
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, alignItems: "center" }}>
          <button onClick={saveScale} disabled={picked.length === 0 || scaleBusy}>
            {scaleBusy ? "Сохранение…" : "Назначить"}
          </button>
          {scaleSaved && <span style={{ color: "#2e7d32", fontSize: 13 }}>Сохранено</span>}
        </div>
        {scaleError && <p className="error">{scaleError}</p>}

        {clinicianScales.length > 0 && (
          <>
            <h4 className="subhead" style={{ marginTop: 14 }}>
              Заполнить шкалу врача (доступно только врачу)
            </h4>
            <select
              aria-label="Шкала врачебной оценки"
              value={assessCode}
              onChange={(e) => {
                setAssessCode(e.target.value);
                setAssessAnswers({});
                setAssessResult(null);
              }}
            >
              {clinicianScales.map((s) => (
                <option key={s.code} value={s.code}>{s.title}</option>
              ))}
            </select>
            {assessScale?.items.map((i) => (
              <label
                key={i.key}
                style={{
                  display: "flex", flexWrap: "wrap", justifyContent: "space-between",
                  alignItems: "center", gap: 8, fontSize: 14,
                }}
              >
                <span style={{ flex: "1 1 260px" }}>{i.text}</span>
                <select
                  aria-label={i.text}
                  value={assessAnswers[i.key] ?? 0}
                  onChange={(e) =>
                    setAssessAnswers((cur) => ({ ...cur, [i.key]: Number(e.target.value) }))
                  }
                  style={{ flex: "0 1 320px", maxWidth: "100%" }}
                >
                  {Array.from({ length: i.max_value + 1 }, (_, v) => v).map((v) => (
                    <option key={v} value={v}>
                      {i.anchors[v] ? `${v} — ${i.anchors[v]}` : String(v)}
                    </option>
                  ))}
                </select>
              </label>
            ))}
            <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 8 }}>
              <button onClick={submitAssessment} disabled={!assessScale || assessBusy}>
                {assessBusy ? "Сохранение…" : "Сохранить оценку"}
              </button>
              {assessResult && (
                <span style={{ fontSize: 13 }}>
                  {assessResult.total_score} · {assessResult.severity} · {assessResult.triage}
                </span>
              )}
            </div>
            {assessErr && <p className="error">{assessErr}</p>}
          </>
        )}
      </Collapsible>

      <Collapsible storageKey="cc.clinician.checkin" summary={checkinSummary}>
        {report.checkin.recent.length === 0 ? (
          <p className="muted small">Пациент ещё не проходил чек-ин.</p>
        ) : (
          <>
            <p className="muted small">Правила: {report.checkin.rules_version ?? "—"}</p>
            {report.checkin.recent.map((c) => {
              const safety =
                c.safety === "signal"
                  ? "срочный сигнал"
                  : c.safety === "review"
                    ? "требует рассмотрения"
                    : c.safety === "unassessed"
                      ? "риск не оценён"
                      : "нет";
              const answers = Object.entries(c.answers)
                .map(([k, v]) => `${k} ${v}`)
                .join(" · ");
              const included = c.modules.filter((m) => m.included);
              return (
                <div key={c.session_id} style={{ marginTop: 10, fontSize: 13 }}>
                  <div>
                    <b>{c.date}</b> · {c.status === "completed" ? "завершён" : "в процессе"} · риск:{" "}
                    <span style={{ color: c.safety === "none" ? "#2f7a4b" : "#a3282d", fontWeight: 600 }}>
                      {safety}
                    </span>
                  </div>
                  {answers && <div style={{ color: "#3d4657" }}>{answers}</div>}
                  {included.length > 0 && (
                    <div style={{ color: "#5a6478" }}>
                      Модули: {included.map((m) => m.code).join(", ")} —{" "}
                      {included.map((m) => m.reason).join("; ")}
                    </div>
                  )}
                </div>
              );
            })}
            <div style={{ color: "#5a6478", fontSize: 12, marginTop: 8 }}>
              Ответы не суммируются и не являются диагнозом: это черновик для пилота.
            </div>
          </>
        )}
      </Collapsible>

      <Collapsible
        storageKey="cc.clinician.adherence"
        summary={
          <>
            Приверженность лечению за последние {report.adherence.days} дней
            {adherencePct != null ? `: ${adherencePct}%` : ""}
          </>
        }
      >
        {report.adherence.medications.length > 0 ? (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Препарат</th><th>Режим</th><th>Принято</th><th>Частично</th>
                    <th>Пропущено</th><th>Не назначено</th><th>Ожидалось</th><th>%</th>
                  </tr>
                </thead>
                <tbody>
                  {report.adherence.medications.map((m) => (
                    <tr key={m.id}>
                      <td>{m.title}</td>
                      <td>{m.times_per_day != null ? `${m.times_per_day} р/день` : "не задан"}</td>
                      <td>{m.taken}</td>
                      <td>{m.partial}</td>
                      <td>{m.missed}</td>
                      <td>{m.not_scheduled}</td>
                      <td>{m.expected}</td>
                      <td>{m.pct != null ? `${m.pct}%` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p style={{ fontSize: 13, marginTop: 6 }}>
              <b>
                Общий процент за последние {report.adherence.days} дней: {adherencePct}%
              </b>
            </p>
            <details className="hint">
              <summary>Как считается процент</summary>
              <p>
                Принято + доля каждого «частично» (1/4 = 0,25, 1/2 = 0,5, 3/4 = 0,75,
                1 из 2 = 0,5, 2 из 3 ≈ 0,67; «меньше/изменил сам» = 0,5), делённое на сумму
                ожидаемых приёмов по назначениям с заданным режимом; это не среднее
                процентов по препаратам. «Не назначено сегодня» в расчёт не входит.
                {report.adherence.excluded_count > 0
                  ? ` Вне расчёта ${report.adherence.excluded_count} отметок без привязки к назначению или у назначений без режима.`
                  : ""}
              </p>
            </details>
            {report.adherence.records.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Дата</th><th>Препарат</th><th>Статус</th><th>Заметка</th></tr></thead>
                  <tbody>
                    {report.adherence.records.map((r) => (
                      <tr key={r.id}>
                        <td>{r.created_at.slice(0, 10)}</td>
                        <td>{r.medication ?? "—"}</td>
                        <td>
                          {ADH_STATUS_LABELS[r.status] ?? r.status}
                          {r.status === "partial" && r.partial_kind
                            ? ` — ${ADH_PARTIAL_LABELS[r.partial_kind] ?? r.partial_kind}`
                            : ""}
                        </td>
                        <td>{r.note ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        ) : (
          <p style={{ color: "#5a6478", fontSize: 13 }}>
            Процент не считается: ни у одного назначения не задан режим приёма (сколько раз в день).
            {report.adherence.total > 0 && ` Отметок за ${report.adherence.days} дней: ${report.adherence.total}, все вне расчёта.`}
          </p>
        )}
      </Collapsible>

      <Collapsible
        storageKey="cc.clinician.adverse"
        summary={
          <>
            Нежелательные явления
            {pendingAeCount > 0 ? ` (${pendingAeCount} на проверке)` : ""}
          </>
        }
      >
        {/* FR-28: единый блок — проверка сообщений пациента и ручное сообщение врача. */}
        {pendingAeCount > 0 && (
          <div className="alert-warn">
            <strong style={{ fontSize: 14 }}>На проверке: {pendingAeCount}</strong>
          </div>
        )}
        {aeList.filter((e) => e.status === "pending").map((event) => (
          <VerifyAdverseEvent
            key={event.id}
            event={event}
            medications={report.adherence.medications}
            drugs={drugs}
            onOpenLibrary={onOpenLibrary}
            onVerified={(updated) =>
              setAeList((cur) => cur.map((e) => (e.id === updated.id ? updated : e)))
            }
          />
        ))}
        <ReportAdverseEvent
          patientId={report.patient_id}
          onCreated={(created) => setAeList((cur) => [created, ...cur])}
        />
        {aeList.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Дата</th><th>Событие</th><th>Тяжесть</th><th>Статус</th><th>Препарат</th><th>Действие</th></tr>
              </thead>
              <tbody>
                {aeList.map((e) => (
                  <tr key={e.id}>
                    <td>{e.onset_date}</td>
                    <td>{e.event_text}</td>
                    <td>
                      {e.severity ? (
                        <span className={`badge ${e.severity === "severe" ? "RED" : e.severity === "moderate" ? "YELLOW" : "GREEN"}`}>
                          {e.severity}
                        </span>
                      ) : (
                        <span className="badge">не оценена</span>
                      )}
                    </td>
                    <td>
                      <span className={`badge ${e.status === "confirmed" ? "GREEN" : e.status === "pending" ? "YELLOW" : ""}`}>
                        {e.status === "confirmed" ? "подтверждено" : e.status === "pending" ? "на проверке" : "отклонено"}
                      </span>
                      {e.reported_by === "patient" && (
                        <div style={{ color: "#5a6478", fontSize: 12 }}>сообщил пациент</div>
                      )}
                    </td>
                    <td>
                      {e.suspected_drug ?? e.drug_trade_name ?? "—"}
                      {e.reference && (
                        <div style={{ color: "#5a6478", fontSize: 12 }} title={e.reference.adverse_events}>
                          ГРЛС: {e.reference.adverse_events.slice(0, 80)}
                          {e.reference.adverse_events.length > 80 ? "…" : ""}
                        </div>
                      )}
                    </td>
                    <td>{e.action_taken ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p style={{ color: "#5a6478", fontSize: 13 }}>Нежелательные явления не зарегистрированы.</p>
        )}
      </Collapsible>

      {report.chat_insights.length > 0 && (
        <>
          <h3>Из переписки</h3>
          <ul className="insight-report">
            {report.chat_insights.map((i) => (
              <li key={i.id}>
                <span className="insight-kind">{INSIGHT_KIND_LABELS[i.kind] ?? i.kind}</span>{" "}
                {i.text}
              </li>
            ))}
          </ul>
          <p className="muted small">
            Это черновик из переписки, не диагноз; проверено врачом{" "}
            {new Date(
              report.chat_insights[0].reviewed_at ?? report.chat_insights[0].created_at,
            ).toLocaleDateString("ru-RU")}
            .
          </p>
        </>
      )}

      <h3 id="clinician-visits">Приёмы (запись беседы врач-пациент)</h3>
      <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 10 }}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <input
            type="date"
            aria-label="Дата приёма"
            value={visitDate}
            onChange={(e) => setVisitDate(e.target.value)}
            style={{ width: 170 }}
          />
          <select
            aria-label="Тип приёма"
            value={visitType}
            onChange={(e) => setVisitType(e.target.value as "in_person" | "online" | "phone")}
          >
            <option value="in_person">Очный</option>
            <option value="online">Онлайн</option>
            <option value="phone">Телефон</option>
          </select>
          <input
            type="number" min={1} max={480} placeholder="Длительность, мин"
            value={visitDuration} onChange={(e) => setVisitDuration(e.target.value)}
            style={{ width: 150 }}
          />
        </div>
        <textarea
          rows={4}
          placeholder="Тема беседы, результаты, план, договорённости…"
          value={visitNotes}
          onChange={(e) => setVisitNotes(e.target.value)}
        />
        <div>
          <button onClick={addVisit} disabled={!visitNotes.trim()}>Сохранить приём</button>
          {visitSaved !== null && <span style={{ marginLeft: 10, color: "#2e7d32" }}>Сохранено</span>}
        </div>
        {visitErr && <p className="error">{visitErr}</p>}
      </div>
      {visits.length > 0 ? (
        <div className="table-wrap" style={{ marginTop: 10 }}>
          <table>
            <thead><tr><th>Дата</th><th>Формат</th><th>Мин</th><th>Запись</th><th></th></tr></thead>
            <tbody>
              {visits.map((v) => (
                <tr key={v.id}>
                  <td>{v.visited_at.slice(0, 10)}</td>
                  <td>{{ in_person: "Очный", online: "Онлайн", phone: "Телефон" }[v.visit_type] ?? v.visit_type}</td>
                  <td>{v.duration_min ?? "—"}</td>
                  <td style={{ whiteSpace: "pre-wrap" }}>{v.notes}</td>
                  <td>
                    <button
                      className="link"
                      style={{ color: "#c62828", fontSize: 12 }}
                      onClick={() => removeVisit(v.id)}
                    >Удалить</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p style={{ color: "#5a6478", fontSize: 13 }}>Записей о приёмах пока нет.</p>
      )}

      <div id="clinician-medications">
        <MedicationsBlock patientId={report.patient_id} />
      </div>

      <div id="clinician-history">
        <HistoryBlock patientId={report.patient_id} />
        <h4 className="subhead">Импорт истории болезни (черновик полей)</h4>
        <p style={{ color: "#5a6478", fontSize: 13 }}>
          Вставьте текст истории болезни (любой шаблон) — система извлечёт ключевые поля
          в черновик. Подтвердите перед использованием в клинической модели.
        </p>
        <textarea rows={5} placeholder="Жалобы: …\nАнамнез: …\nДиагноз: F41.1\nЛечение: Авиандр 20 мг"
          value={histText} onChange={(e) => setHistText(e.target.value)} />
        <div style={{ marginTop: 8 }}>
          <button onClick={runImport} disabled={!histText.trim()}>Разобрать</button>
        </div>
        {histErr && <p className="error">{histErr}</p>}
        {histResult && (
          <div className="alert-info" style={{ marginTop: 10 }}>
            <b>Черновик (требует подтверждения)</b> · шаблон: {histResult.template}, полей: {histResult.fields_found}
            {histResult.complaints && <div><b>Жалобы:</b> {histResult.complaints}</div>}
            {histResult.anamnesis && <div><b>Анамнез:</b> {histResult.anamnesis}</div>}
            {histResult.diagnosis_codes.length > 0 && <div><b>Коды МКБ-10:</b> {histResult.diagnosis_codes.join(", ")}</div>}
            {histResult.therapy.length > 0 && <div><b>Терапия:</b> {histResult.therapy.join("; ")}</div>}
            {histResult.scale_scores.length > 0 && <div><b>Баллы шкал:</b> {histResult.scale_scores.join("; ")}</div>}
          </div>
        )}
      </div>

      <Collapsible
        storageKey="cc.clinician.dynamics"
        summary="Динамика (90 дней)"
        defaultOpen={false}
      >
        <TrendChart timeline={{ runs: report.history, trend: report.trend }} />
      </Collapsible>

      <Collapsible
        storageKey="cc.clinician.screening-history"
        summary="История скринингов"
        defaultOpen={false}
      >
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Дата</th>
                <th>Балл</th>
                <th>Severity</th>
                <th>Риск</th>
              </tr>
            </thead>
            <tbody>
              {report.history.map((h, i) => (
                <tr key={i}>
                  <td>{h.computed_at.slice(0, 10)}</td>
                  <td>{h.total_score}</td>
                  <td>{h.severity}</td>
                  <td>{h.triage}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Collapsible>

      <h3>Дневник пациента</h3>
      {report.diary.length === 0 && <p style={{ color: "#5a6478" }}>Записей нет</p>}
      {report.diary.map((d) => (
        <div key={d.id} className="msg">
          <b>{d.created_at.slice(0, 10)}: </b>
          {d.content}
          {d.mood != null
            ? ` (настроение ${d.mood}/${d.mood_scale === "0-4" ? 4 : 10})`
            : ""}
        </div>
      ))}
    </div>
  );
}

// FR-28: ручное сообщение врача о НЯ. Живёт внутри блока проверки сообщений
// пациента — второй конкурирующей формы «зарегистрировать НЯ» в карточке нет.
function ReportAdverseEvent({ patientId, onCreated }: {
  patientId: string;
  onCreated: (created: ClinicianAdverseEvent) => void;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [severity, setSeverity] = useState<AdverseSeverity>("mild");
  const [onsetDate, setOnsetDate] = useState(new Date().toISOString().slice(0, 10));
  const [drug, setDrug] = useState("");
  const [action, setAction] = useState("monitoring");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    if (!text.trim()) return;
    setErr("");
    setBusy(true);
    try {
      const created = await api.createAdverseEvent(patientId, {
        event_text: text.trim(),
        severity,
        onset_date: onsetDate,
        suspected_drug: drug.trim() || undefined,
        action_taken: action,
      });
      onCreated(created);
      setText("");
      setDrug("");
      setSaved(true);
      setOpen(false);
    } catch (e: unknown) {
      setErr(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <button className="ghost" onClick={() => setOpen((v) => !v)}>
          {open ? "Свернуть сообщение о НЯ" : "Сообщить о НЯ"}
        </button>
        {saved && <span style={{ color: "#2e7d32", fontSize: 13 }}>Сохранено</span>}
      </div>
      {open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, maxWidth: 560, marginTop: 8 }}>
          <textarea
            rows={2}
            placeholder="Описание НЯ"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <select
              aria-label="Тяжесть НЯ"
              value={severity}
              onChange={(e) => setSeverity(e.target.value as AdverseSeverity)}
            >
              <option value="mild">лёгкое</option>
              <option value="moderate">умеренное</option>
              <option value="severe">тяжёлое</option>
            </select>
            <input
              type="date"
              aria-label="Дата начала НЯ"
              value={onsetDate}
              onChange={(e) => setOnsetDate(e.target.value)}
              style={{ width: "auto" }}
            />
            <input
              placeholder="Препарат (напр. Авиандр)"
              value={drug}
              onChange={(e) => setDrug(e.target.value)}
            />
            <select
              aria-label="Действие при НЯ"
              value={action}
              onChange={(e) => setAction(e.target.value)}
            >
              <option value="none">нет действия</option>
              <option value="monitoring">наблюдение</option>
              <option value="dose_reduced">снижение дозы</option>
              <option value="stopped">отмена</option>
              <option value="referred">направление</option>
            </select>
          </div>
          <div>
            <button onClick={submit} disabled={busy || !text.trim()}>
              Сохранить НЯ
            </button>
          </div>
          {err && <p className="error">{err}</p>}
        </div>
      )}
    </div>
  );
}
// FR-28: проверка сообщения пациента. Решение выбирается кнопкой, поля
// (тяжесть, назначение, препарат ГРЛС, действие, заметка) применяются при
// сохранении. После сохранения строка уходит из очереди «на проверке».
function VerifyAdverseEvent({ event, medications, drugs, onOpenLibrary, onVerified }: {
  event: ClinicianAdverseEvent;
  medications: Report["adherence"]["medications"];
  drugs: Drug[];
  onOpenLibrary: () => void;
  onVerified: (updated: ClinicianAdverseEvent) => void;
}) {
  const [decision, setDecision] = useState<"confirmed" | "rejected">("confirmed");
  const [severity, setSeverity] = useState<AdverseSeverity | "">(event.severity ?? "");
  const [medId, setMedId] = useState(event.medication_id ?? "");
  const [drugId, setDrugId] = useState(event.drug_id ?? "");
  const [action, setAction] = useState(event.action_taken ?? "monitoring");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const drug = drugs.find((d) => d.drug_id === drugId);
  // Подтвердить НЯ без явно выбранной тяжести нельзя: severity=null выглядел бы
  // как «тяжесть не оценена», хотя врач событие подтвердил. Отказ тяжести не требует.
  const needsSeverity = decision === "confirmed" && !severity;

  const save = async () => {
    if (needsSeverity) return;
    setErr("");
    setBusy(true);
    try {
      onVerified(await api.verifyAdverseEvent(event.patient_id, event.id, {
        status: decision,
        severity: severity || undefined,
        medication_id: medId || undefined,
        drug_id: drugId || undefined,
        action_taken: action,
        verification_note: note.trim() || undefined,
      }));
    } catch (e: unknown) {
      setErr(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="msg"
      style={{ borderLeft: `3px solid ${event.severity === "severe" ? "#d64545" : event.severity ? "#c9a227" : "#cfd6e2"}`, paddingLeft: 10 }}
    >
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", fontSize: 13 }}>
        <b>{event.onset_date}</b>
        <span className={`badge ${event.severity === "severe" ? "RED" : event.severity === "moderate" ? "YELLOW" : event.severity ? "GREEN" : ""}`}>
          {event.severity ?? "не оценена"}
        </span>
        <span>{event.event_text}</span>
        {event.medication_title && (
          <span style={{ color: "#5a6478" }}>· назначение: {event.medication_title}</span>
        )}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", marginTop: 8 }}>
        <button
          className={decision === "confirmed" ? "active" : ""}
          aria-pressed={decision === "confirmed"}
          onClick={() => setDecision("confirmed")}
        >
          Подтвердить
        </button>
        <button
          className={decision === "rejected" ? "active" : ""}
          aria-pressed={decision === "rejected"}
          onClick={() => setDecision("rejected")}
        >
          Отклонить
        </button>
        <select
          aria-label="Тяжесть при проверке"
          aria-invalid={needsSeverity || undefined}
          value={severity}
          onChange={(e) => setSeverity(e.target.value as AdverseSeverity | "")}
        >
          <option value="" disabled>выберите тяжесть</option>
          <option value="mild">лёгкое</option>
          <option value="moderate">умеренное</option>
          <option value="severe">тяжёлое</option>
        </select>
        <select aria-label="Связь с назначением" value={medId} onChange={(e) => setMedId(e.target.value)}>
          <option value="">назначение не выбрано</option>
          {medications.map((m) => (
            <option key={m.id} value={m.id}>{m.title}</option>
          ))}
        </select>
        <select aria-label="Препарат из справочника" value={drugId} onChange={(e) => setDrugId(e.target.value)}>
          <option value="">справочник ГРЛС</option>
          {drugs.map((d) => (
            <option key={d.drug_id} value={d.drug_id}>{d.trade_name}</option>
          ))}
        </select>
        <select aria-label="Действие при проверке" value={action} onChange={(e) => setAction(e.target.value)}>
          <option value="none">действий не требуется</option>
          <option value="monitoring">наблюдение</option>
          <option value="dose_reduced">доза снижена</option>
          <option value="stopped">препарат отменён</option>
          <option value="referred">направлен к врачу</option>
        </select>
        <input placeholder="Заметка врача" value={note} onChange={(e) => setNote(e.target.value)} style={{ maxWidth: 260 }} />
        <button onClick={save} disabled={busy || needsSeverity}>
          {busy ? "Сохранение…" : "Сохранить проверку"}
        </button>
        {needsSeverity && (
          <span className="small" style={{ color: "#a3282d" }}>
            Укажите тяжесть, чтобы подтвердить НЯ
          </span>
        )}
      </div>
      {drug && (
        <p style={{ fontSize: 12, color: "#5a6478", marginBottom: 0 }}>
          В справочнике ГРЛС: {drug.adverse_events.slice(0, 240)}
          {drug.adverse_events.length > 240 ? "…" : ""}{" "}
          <a href="#library" onClick={onOpenLibrary} style={{ color: "#2f6fed" }}>
            открыть справочник
          </a>
        </p>
      )}
      {err && <p className="error">{err}</p>}
    </div>
  );
}
function AnamnesisBlock({
  patientId,
  initial,
}: {
  patientId: string;
  initial: Report["anamnesis"];
}) {
  const [data, setData] = useState(initial);
  const [brief, setBrief] = useState(initial?.brief ?? "");
  const [draft, setDraft] = useState<{ draft: string; masked_report: { applied: boolean; counts: Record<string, number> } } | null>(null);
  const [draftText, setDraftText] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState("");

  if (!initial) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <h3 style={{ margin: 0 }}>Анамнез</h3>
        <p style={{ color: "#5a6478", fontSize: 13, margin: 0 }}>
          Нет активного согласия пациента — анамнез недоступен (152-ФЗ).
        </p>
      </div>
    );
  }

  const generate = async () => {
    setBusy(true);
    setErr("");
    setSaved(false);
    try {
      const res = await api.clinicianAnamnesisDraft(patientId, brief);
      setDraft(res);
      setDraftText(res.draft);
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  const saveAnamnesis = async () => {
    setBusy(true);
    setErr("");
    setSaved(false);
    try {
      const updated = await api.clinicianAnamnesisSave(patientId, {
        brief,
        ...(draft ? { text: draftText } : {}),
      });
      setData(updated);
      setBrief(updated.brief ?? "");
      setDraft(null);
      setDraftText("");
      setEditing(false);
      setSaved(true);
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  const cancel = () => {
    setBrief(data?.brief ?? "");
    setDraft(null);
    setDraftText("");
    setErr("");
    setEditing(false);
  };

  const masked = draft
    ? Object.entries(draft.masked_report.counts)
        .map(([kind, count]) => `${kind}: ${count}`)
        .join(", ")
    : "";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <h3 style={{ margin: 0 }}>Анамнез</h3>
      {!editing && (
        <>
          {data?.text ? (
            <div style={{ whiteSpace: "pre-wrap", fontSize: 14, lineHeight: 1.5 }}>{data.text}</div>
          ) : (
            <p style={{ color: "#5a6478", fontSize: 13, margin: 0 }}>
              Анамнез не заполнен.
            </p>
          )}
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <button className="ghost" onClick={() => { setEditing(true); setSaved(false); }}>
              Редактировать
            </button>
            {data?.updated_at && (
              <span style={{ color: "#5a6478", fontSize: 12 }}>
                обновлён {data.updated_at.slice(0, 10)}
              </span>
            )}
            {saved && <span style={{ color: "#2f7a4b", fontSize: 12 }}>сохранено</span>}
          </div>
        </>
      )}
      {editing && (
        <>
          <label htmlFor="anamnesis-brief" style={{ fontSize: 14 }}>
            Краткие вводные
          </label>
          <textarea
            id="anamnesis-brief"
            rows={4}
            maxLength={4000}
            value={brief}
            placeholder="Возраст, жалобы, динамика, значимые события (без ФИО и контактов)"
            onChange={(e) => setBrief(e.target.value)}
          />
          <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <button onClick={generate} disabled={busy}>
              {busy ? "Формирование…" : "Сформировать черновик (ИИ)"}
            </button>
            <button onClick={saveAnamnesis} disabled={busy}>
              Сохранить
            </button>
            <button className="ghost" onClick={cancel} disabled={busy}>
              Отмена
            </button>
            {draft && (
              <span style={{ color: "#5a6478", fontSize: 12 }}>
                перед отправкой замаскировано: {masked || "нет"}
              </span>
            )}
          </div>
          {draft && (
            <>
              <p style={{ color: "#5a6478", fontSize: 12, margin: 0 }}>
                Черновик модели — проверьте и отредактируйте перед сохранением. В карточку
                попадёт только после «Сохранить».
              </p>
              <textarea
                rows={12}
                maxLength={20000}
                value={draftText}
                onChange={(e) => setDraftText(e.target.value)}
              />
            </>
          )}
          {err && <p className="error">{err}</p>}
        </>
      )}
    </div>
  );
}

// (аудит READ_PATIENT_CONTACTS); в списке и карте телефон маскирован.
function ContactsBlock({ patientId, hasConsent }: { patientId: string; hasConsent: boolean }) {
  const [contacts, setContacts] = useState<PatientContacts | null>(null);
  const [phone, setPhone] = useState("");
  const [birthDate, setBirthDate] = useState("");
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!hasConsent) return;
    api
      .clinicianContacts(patientId)
      .then((c) => {
        setContacts(c);
        setPhone(c.phone ?? "");
        setBirthDate(c.birth_date ?? "");
      })
      .catch((e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, [patientId, hasConsent]);

  if (!hasConsent) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <h3 style={{ margin: 0 }}>Контакты</h3>
        <p style={{ color: "#5a6478", fontSize: 13, margin: 0 }}>
          Нет активного согласия пациента — контакты недоступны (152-ФЗ).
        </p>
      </div>
    );
  }

  const save = async () => {
    setErr("");
    setSaved(false);
    try {
      const c = await api.clinicianContactsUpdate(patientId, {
        phone: phone.trim() === "" ? null : phone.trim(),
        birth_date: birthDate.trim() === "" ? null : birthDate.trim(),
      });
      setContacts(c);
      setPhone(c.phone ?? "");
      setBirthDate(c.birth_date ?? "");
      setSaved(true);
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e));
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <h3 style={{ margin: 0 }}>Контакты</h3>
      <div className="row" style={{ alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <label htmlFor="contact-phone" style={{ fontSize: 14 }}>Телефон</label>
        <input
          id="contact-phone"
          value={phone}
          placeholder="+7XXXXXXXXXX"
          onChange={(e) => setPhone(e.target.value)}
          style={{ maxWidth: 220 }}
        />
        <label htmlFor="contact-birth" style={{ fontSize: 14 }}>Дата рождения</label>
        <input
          id="contact-birth"
          type="date"
          value={birthDate}
          onChange={(e) => setBirthDate(e.target.value)}
          style={{ maxWidth: 180 }}
        />
        <button onClick={save}>Сохранить контакты</button>
        {saved && <span style={{ color: "#2e7d32", fontSize: 13 }}>Сохранено</span>}
      </div>
      {contacts && (
        <div style={{ color: "#5a6478", fontSize: 13 }}>
          {contacts.age !== null && <>Возраст: {contacts.age}</>}
          {contacts.adult === false && <> · несовершеннолетний</>}
        </div>
      )}
      {err && <p className="error">{err}</p>}
    </div>
  );
}
// FR-23: назначения препаратов в карточке. Импорт ЕМИАС — только чтение,
// локальные назначения врач останавливает/удаляет.
function MedicationsBlock({ patientId }: { patientId: string }) {
  const [items, setItems] = useState<PatientMedication[]>([]);
  const [title, setTitle] = useState("");
  const [dosage, setDosage] = useState("");
  const [startedOn, setStartedOn] = useState(new Date().toISOString().slice(0, 10));
  const [notes, setNotes] = useState("");
  // FR-27: режим приёма — знаменатель приверженности (1..6 раз в день).
  const [timesPerDay, setTimesPerDay] = useState(1);
  const [editId, setEditId] = useState<string | null>(null);
  const [editTimes, setEditTimes] = useState(1);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  // Защита от гонки: ответ устаревшего запроса не перезаписывает свежий список.
  const reqRef = useRef(0);
  // FR-32: автодополнение препарата из справочника ГРЛС (GET /drugs).
  const [drugs, setDrugs] = useState<Drug[]>([]);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [suggestIdx, setSuggestIdx] = useState(-1);
  // Подсказка дозировки: препарат, выбранный из справочника (dosage → special_notes → form).
  const [pickedDrug, setPickedDrug] = useState<Drug | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.drugs()
      .then((r) => { if (!cancelled) setDrugs(r.items); })
      .catch(() => { if (!cancelled) setDrugs([]); });
    return () => { cancelled = true; };
  }, []);

  const drugQuery = title.trim().toLowerCase();
  const suggestions = drugQuery.length >= 2
    ? drugs
        .filter((d) =>
          [d.trade_name, d.inn, d.inn_en ?? ""].some((v) => v.toLowerCase().includes(drugQuery)),
        )
        .slice(0, 8)
    : [];

  const pickDrug = (d: Drug) => {
    setTitle(d.trade_name);
    setPickedDrug(d);
    setSuggestOpen(false);
    setSuggestIdx(-1);
  };

  // Источник канонической дозировки: dosage, при его отсутствии — special_notes или форма.
  const dosageHint = pickedDrug
    ? pickedDrug.dosage.trim() || pickedDrug.special_notes.trim() || pickedDrug.form.trim()
    : "";
  const dosageApplied = dosageHint !== "" && dosage.trim() === dosageHint;

  const onDrugKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      setSuggestOpen(false);
      setSuggestIdx(-1);
      return;
    }
    if (suggestions.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSuggestOpen(true);
      setSuggestIdx((i) => (i + 1) % suggestions.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSuggestOpen(true);
      setSuggestIdx((i) => (i <= 0 ? suggestions.length - 1 : i - 1));
    } else if (e.key === "Enter" && suggestOpen && suggestIdx >= 0) {
      // Enter выбирает подсказку, а не отправляет форму.
      e.preventDefault();
      pickDrug(suggestions[suggestIdx]);
    }
  };

  const load = () => {
    const token = ++reqRef.current;
    api.clinicianMedications(patientId)
      .then((r) => { if (token === reqRef.current) setItems(r.items); })
      .catch((e) => { if (token === reqRef.current) setErr(e instanceof ApiError ? e.detail : String(e)); });
  };
  useEffect(() => {
    reqRef.current += 1;
    setItems([]);
    setErr("");
    load();
  }, [patientId]);

  const add = async () => {
    if (!title.trim()) return;
    setBusy(true);
    setErr("");
    try {
      await api.createMedication(patientId, {
        title: title.trim(),
        dosage: dosage.trim() || undefined,
        started_on: startedOn || undefined,
        times_per_day: timesPerDay,
        notes: notes.trim() || undefined,
      });
      setTitle("");
      setDosage("");
      setPickedDrug(null);
      setNotes("");
      setTimesPerDay(1);
      load();
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  const stop = async (medId: string) => {
    setErr("");
    try {
      await api.stopMedication(patientId, medId);
      load();
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const remove = async (medId: string) => {
    if (!window.confirm("Удалить назначение? Запись будет удалена без возможности восстановления.")) return;
    setErr("");
    try {
      await api.deleteMedication(patientId, medId);
      load();
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const saveTimes = async (medId: string) => {
    setErr("");
    try {
      await api.updateMedication(patientId, medId, { times_per_day: editTimes });
      setEditId(null);
      load();
    } catch (e) {
      setErr(e instanceof ApiError ? e.detail : String(e));
    }
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <h3>Назначения (FR-23)</h3>
      {items.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Препарат</th><th>Дозировка</th><th>Режим</th><th>С</th><th>По</th><th>Статус</th><th>Заметка</th><th></th></tr>
            </thead>
            <tbody>
              {items.map((m) => (
                <tr key={m.id}>
                  <td>
                    {m.title}{" "}
                    {m.source === "emias" && (
                      <span style={{
                        fontSize: 11, color: "#5a6478", border: "1px solid #cfd6e4",
                        borderRadius: 4, padding: "0 4px", whiteSpace: "nowrap",
                      }}>ЕМИАС</span>
                    )}
                  </td>
                  <td>{m.dosage ?? "—"}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {editId === m.id ? (
                      <>
                        <select
                          aria-label="Раз в день"
                          value={editTimes}
                          onChange={(e) => setEditTimes(Number(e.target.value))}
                        >
                          {[1, 2, 3, 4, 5, 6].map((n) => <option key={n} value={n}>{n}</option>)}
                        </select>
                        <button className="link" style={{ fontSize: 12 }} onClick={() => saveTimes(m.id)}>
                          Сохранить
                        </button>
                      </>
                    ) : m.times_per_day != null ? (
                      `${m.times_per_day} р/день`
                    ) : (
                      <span style={{ color: "#5a6478", fontSize: 12 }}>не задан</span>
                    )}
                  </td>
                  <td>{m.started_on ?? "—"}</td>
                  <td>{m.ended_on ?? "—"}</td>
                  <td>{MED_STATUS_LABELS[m.status] ?? m.status}</td>
                  <td style={{ whiteSpace: "pre-wrap" }}>{m.notes ?? "—"}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {m.editable ? (
                      <>
                        {m.status !== "stopped" && (
                          <button className="link" style={{ fontSize: 12 }} onClick={() => stop(m.id)}>
                            Остановить
                          </button>
                        )}
                        {m.editable && editId !== m.id && m.source === "clinician" && (
                          <button
                            className="link"
                            style={{ fontSize: 12, marginLeft: 8 }}
                            onClick={() => { setEditId(m.id); setEditTimes(m.times_per_day ?? 1); }}
                          >Режим</button>
                        )}
                        <button
                          className="link"
                          style={{ color: "#c62828", fontSize: 12, marginLeft: 8 }}
                          onClick={() => remove(m.id)}
                        >Удалить</button>
                      </>
                    ) : (
                      <span style={{ color: "#5a6478", fontSize: 12 }}>только чтение</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p style={{ color: "#5a6478", fontSize: 13 }}>Назначений пока нет.</p>
      )}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", maxWidth: 760 }}>
        <div className="drug-autocomplete">
          <input
            placeholder="Препарат"
            role="combobox"
            aria-label="Препарат"
            aria-autocomplete="list"
            aria-expanded={suggestOpen && suggestions.length > 0}
            aria-controls="drug-suggestions"
            aria-activedescendant={
              suggestOpen && suggestIdx >= 0 ? `drug-option-${suggestIdx}` : undefined
            }
            autoComplete="off"
            value={title}
            onChange={(e) => {
              setTitle(e.target.value);
              setPickedDrug(null);
              setSuggestOpen(true);
              setSuggestIdx(-1);
            }}
            onFocus={() => setSuggestOpen(true)}
            onBlur={() => window.setTimeout(() => setSuggestOpen(false), 120)}
            onKeyDown={onDrugKeyDown}
          />
          {suggestOpen && suggestions.length > 0 && (
            <ul
              id="drug-suggestions"
              className="drug-suggestions"
              role="listbox"
              aria-label="Препараты из справочника"
            >
              {suggestions.map((d, idx) => (
                <li
                  key={d.drug_id}
                  id={`drug-option-${idx}`}
                  role="option"
                  aria-selected={idx === suggestIdx}
                  className={`drug-suggestion${idx === suggestIdx ? " active" : ""}`}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    pickDrug(d);
                  }}
                >
                  <Highlight text={d.trade_name} query={drugQuery} />
                  <span className="muted">
                    {d.inn}
                    {d.inn_en ? ` · ${d.inn_en}` : ""}
                    {d.form ? ` · ${d.form}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="dosage-field">
          <input placeholder="Дозировка" value={dosage} onChange={(e) => setDosage(e.target.value)} style={{ maxWidth: 150 }} />
          {dosageHint && (
            <div className="dosage-hint" title={dosageHint}>
              <span className="dosage-hint-text">Справочник: {dosageHint}</span>
              {!dosageApplied && (
                <button type="button" className="ghost" onClick={() => setDosage(dosageHint)}>
                  Подставить
                </button>
              )}
            </div>
          )}
        </div>
        <input type="date" aria-label="Дата начала" value={startedOn} onChange={(e) => setStartedOn(e.target.value)} />
        <label style={{ fontSize: 13, color: "#3d4657" }}>
          Раз в день:{" "}
          <select aria-label="Раз в день" value={timesPerDay} onChange={(e) => setTimesPerDay(Number(e.target.value))}>
            {[1, 2, 3, 4, 5, 6].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <input placeholder="Заметка" value={notes} onChange={(e) => setNotes(e.target.value)} style={{ maxWidth: 240 }} />
        <button onClick={add} disabled={busy || !title.trim()}>Назначить</button>
      </div>
      {err && <p className="error">{err}</p>}
    </div>
  );
}
const HISTORY_CHIPS: { value: HistoryType; label: string }[] = [
  { value: "visit", label: "Приёмы" },
  { value: "medication", label: "Назначения" },
  { value: "screening", label: "Скрининги" },
  { value: "adverse", label: "Нежелательные явления" },
];

const HISTORY_LABELS: Record<HistoryType, string> = {
  visit: "приём",
  medication: "назначение",
  screening: "скрининг",
  adverse: "НЯ",
};

const HISTORY_SOURCE_LABELS: Record<string, string> = {
  clinician: "врач",
  emias: "ЕМИАС",
  patient: "пациент",
};

// FR-23: единая хронология карточки. Фильтр по типам — чипы, подгрузка курсором.
function HistoryBlock({ patientId }: { patientId: string }) {
  const [types, setTypes] = useState<HistoryType[]>(HISTORY_CHIPS.map((c) => c.value));
  const [items, setItems] = useState<PatientHistoryItem[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const key = types.join(",");
  // Смена пациента или фильтра инвалидирует старые ответы и курсор.
  const reqRef = useRef(0);

  const load = async (before?: string) => {
    const token = ++reqRef.current;
    if (types.length === 0) {
      setItems([]);
      setNext(null);
      return;
    }
    setLoading(true);
    setErr("");
    try {
      const r = await api.clinicianHistory(patientId, { types: key, limit: 50, before });
      if (token !== reqRef.current) return;
      setItems((prev) => (before ? [...prev, ...r.items] : r.items));
      setNext(r.next_before);
    } catch (e) {
      if (token !== reqRef.current) return;
      setErr(e instanceof ApiError ? e.detail : String(e));
    } finally {
      if (token === reqRef.current) setLoading(false);
    }
  };

  useEffect(() => {
    reqRef.current += 1;
    setItems([]);
    setNext(null);
    setErr("");
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patientId, key]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <h3>История пациента (FR-23)</h3>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        {HISTORY_CHIPS.map((chip) => {
          const on = types.includes(chip.value);
          return (
            <button
              key={chip.value}
              onClick={() => setTypes((prev) => (
                prev.includes(chip.value) ? prev.filter((x) => x !== chip.value) : [...prev, chip.value]
              ))}
              aria-pressed={on}
              style={{
                fontSize: 13, padding: "2px 10px", borderRadius: 12,
                border: "1px solid #cfd6e4", cursor: "pointer",
                background: on ? "#e8eefc" : "transparent",
                color: on ? "#1b3a8a" : "#5a6478",
              }}
            >{chip.label}</button>
          );
        })}
        <button className="link" style={{ fontSize: 13 }} onClick={() => load()} disabled={loading}>
          Обновить
        </button>
      </div>
      {items.length > 0 ? (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          {items.map((it) => (
            <li key={`${it.type}:${it.ref_id}`} style={{ borderLeft: "3px solid #cfd6e4", paddingLeft: 10 }}>
              <div style={{ fontSize: 12, color: "#5a6478" }}>
                {it.at.slice(0, 16).replace("T", " ")} · {HISTORY_LABELS[it.type]} ·{" "}
                {HISTORY_SOURCE_LABELS[it.source] ?? it.source}
              </div>
              <div style={{ fontWeight: 600 }}>{it.title}</div>
              <div style={{ fontSize: 13, whiteSpace: "pre-wrap" }}>{it.summary}</div>
            </li>
          ))}
        </ul>
      ) : (
        <p style={{ color: "#5a6478", fontSize: 13 }}>
          {types.length === 0
            ? "Выберите хотя бы один тип событий."
            : loading ? "Загрузка…" : "Событий нет."}
        </p>
      )}
      {next && (
        <div>
          <button onClick={() => load(next)} disabled={loading}>Показать ещё</button>
        </div>
      )}
      {err && <p className="error">{err}</p>}
    </div>
  );
}
// FR-24: напоминания выбранного пациента — прямо в карточке (п.3 фидбека).
function PatientReminders({ patientId }: { patientId: string }) {
  const [data, setData] = useState<RemindersResponse>({ items: [], overdue: 0 });
  const [formOpen, setFormOpen] = useState(false);
  const [kind, setKind] = useState<ClinicianReminder["kind"]>("call");
  const [note, setNote] = useState("");
  const [due, setDue] = useState(nextHourLocal);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = () => {
    api
      .clinicianPatientReminders(patientId)
      .then((r) => {
        setData(r);
        setErr("");
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  };

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patientId]);

  const create = async () => {
    if (!due) {
      setErr("Укажите срок");
      return;
    }
    setBusy(true);
    try {
      await api.createReminder({
        patient_id: patientId,
        kind,
        note: note.trim() || null,
        due_at: new Date(due).toISOString(),
      });
      setNote("");
      setFormOpen(false);
      reload();
    } catch (e: unknown) {
      setErr(e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      reload();
    } catch (e: unknown) {
      setErr(e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const snooze = (r: ClinicianReminder) => {
    const next = new Date(r.due_at);
    next.setDate(next.getDate() + 1);
    return act(() => api.patchReminder(r.id, { due_at: next.toISOString() }));
  };

  return (
    <div className="reminders-block">
      <div className="row">
        <button className="ghost" onClick={() => setFormOpen((v) => !v)}>
          {formOpen ? "Скрыть форму" : "Добавить напоминание"}
        </button>
        <span className="muted small">
          {data.overdue > 0 ? `Просрочено: ${data.overdue}` : "Просроченных нет"}
        </span>
      </div>
      {formOpen && (
        <div className="reminder-form">
          <select
            aria-label="Вид напоминания"
            value={kind}
            onChange={(e) => setKind(e.target.value as ClinicianReminder["kind"])}
          >
            {REMINDER_KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {k.label}
              </option>
            ))}
          </select>
          <input
            type="datetime-local"
            aria-label="Срок напоминания"
            value={due}
            onChange={(e) => setDue(e.target.value)}
          />
          <input
            placeholder="Комментарий"
            aria-label="Комментарий к напоминанию"
            value={note}
            maxLength={2000}
            onChange={(e) => setNote(e.target.value)}
          />
          <button onClick={() => void create()} disabled={busy || !due}>
            Создать
          </button>
        </div>
      )}
      {data.items.length === 0 ? (
        <p className="muted small">Напоминаний по пациенту нет.</p>
      ) : (
        <ul className="reminder-list">
          {data.items.map((r) => (
            <li key={r.id} className={`reminder-item ${r.due_state ?? ""}`}>
              <div className="reminder-main">
                <strong>{r.kind_label}</strong>
                <span className="muted small"> · {fmtDue(r.due_at)}</span>
                {r.due_state && (
                  <span className="reminder-state">{REMINDER_STATE_LABELS[r.due_state]}</span>
                )}
                {r.note && <div className="reminder-note">{r.note}</div>}
              </div>
              <div className="reminder-actions">
                <button className="ghost" disabled={busy} onClick={() => void act(() => api.doneReminder(r.id))}>
                  Выполнено
                </button>
                <button className="ghost" disabled={busy} onClick={() => void snooze(r)}>
                  Сдвинуть на день
                </button>
                <button className="ghost" disabled={busy} onClick={() => void act(() => api.cancelReminder(r.id))}>
                  Отменить
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {err && <p className="error">{err}</p>}
    </div>
  );
}
