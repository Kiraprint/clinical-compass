import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, Assessment, ClinicianReminder, EmiasSummary, Escalation, HistoryType, ImportHistoryResult, PatientCard, PatientContacts, PatientHistoryItem, PatientMedication, RemindersResponse, Report, Visit } from "../api";
import TrendChart from "../TrendChart";
import AssistantTab from "./AssistantTab";
import DrugLibraryTab from "./DrugLibraryTab";

const MED_STATUS_LABELS: Record<string, string> = {
  active: "активно",
  completed: "завершено",
  stopped: "отменено",
};


// Сворачиваемый блок карточки врача: состояние раскрытия запоминаем в localStorage,
// чтобы врач не терял привычный вид между визитами (отзыв эксперта: шкала Гамильтона
// занимает весь экран).
function Collapsible({
  storageKey,
  summary,
  children,
}: {
  storageKey: string;
  summary: ReactNode;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem(storageKey) !== "0";
    } catch {
      return true;
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
      <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: 16, marginBottom: 8 }}>
        {summary}
      </summary>
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
  if (items.length === 0) return null;
  return (
    <div className="card">
      <h2>Очередь эскалаций ({items.length})</h2>
      <p style={{ color: "#5a6478", fontSize: 13 }}>
        Открытые сигналы чек-инов и скринингов, включая пациентов без закреплённого врача.
      </p>
      {error && <p style={{ color: "#b3261e", fontSize: 13 }}>{error}</p>}
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
    </div>
  );
}


export default function ClinicianView() {
  const [section, setSection] = useState<"patients" | "assistant" | "library">("patients");
  const [filter, setFilter] = useState<(typeof TRIAGE_FILTERS)[number]>("");
  const [cards, setCards] = useState<PatientCard[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [reportError, setReportError] = useState("");
  // Счётчик перезагрузки: врачебная оценка меняет триаж/эскалации, и карточка
  // с списком должны показать это сразу, без ручного F5.
  const [reportVersion, setReportVersion] = useState(0);
  // FR-24: панель напоминаний и бейдж просроченных в шапке врача.
  const [showReminders, setShowReminders] = useState(false);
  const [overdueCount, setOverdueCount] = useState(0);
  const [reminderVersion, setReminderVersion] = useState(0);
  // Общая очередь эскалаций — не только свои пациенты (docs/16 §3).
  const [escalations, setEscalations] = useState<Escalation[]>([]);
  useEffect(() => {
    api
      .clinicianReminders()
      .then((r) => setOverdueCount(r.overdue))
      .catch(() => setOverdueCount(0));
  }, [reminderVersion, reportVersion]);

  useEffect(() => {
    if (selected) return;
    let cancelled = false;
    api
      .openEscalations()
      .then((r) => {
        if (!cancelled) setEscalations(r.items);
      })
      .catch(() => {
        if (!cancelled) setEscalations([]);
      });
    return () => {
      cancelled = true;
    };
  }, [selected, reportVersion]);

  const load = async (f: string) => {
    const data = await api.clinicianPatients(f || undefined);
    setCards(data.items);
  };

  useEffect(() => {
    load(filter);
  }, [filter, reportVersion]);

  // Карточка пациента — отдельная ссылка (#patient=<id>): синхронизируем с hash,
  // чтобы клик по уведомлению об эскалации открывал нужного пациента.
  useEffect(() => {
    const applyHash = () => {
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

  if (section === "assistant") {
    return (
      <>
        <div className="tabs">
          <button onClick={() => setSection("patients")}>Пациенты</button>
          <button className="active" onClick={() => setSection("assistant")}>
            Ассистент
          </button>
          <button onClick={() => setSection("library")}>Библиотека</button>
        </div>
        <AssistantTab />
      </>
    );
  }

  if (section === "library") {
    return (
      <>
        <div className="tabs">
          <button onClick={() => setSection("patients")}>Пациенты</button>
          <button onClick={() => setSection("assistant")}>Ассистент</button>
          <button className="active" onClick={() => setSection("library")}>
            Библиотека
          </button>
        </div>
        <DrugLibraryTab />
      </>
    );
  }

  return (
    <>
      <div className="tabs">
        <button className="active" onClick={() => setSection("patients")}>
          Пациенты
        </button>
        <button onClick={() => setSection("assistant")}>Ассистент</button>
        <button onClick={() => setSection("library")}>Библиотека</button>
        <button
          className={showReminders ? "active" : ""}
          onClick={() => setShowReminders((v) => !v)}
          style={{ marginLeft: "auto" }}
        >
          Напоминания
          {overdueCount > 0 && (
            <span
              style={{
                marginLeft: 6,
                background: "#d32f2f",
                color: "#fff",
                borderRadius: 8,
                padding: "0 6px",
                fontSize: 12,
              }}
            >
              {overdueCount}
            </span>
          )}
        </button>
      </div>
      <div className="tabs">
        {TRIAGE_FILTERS.map((f) => (
          <button key={f} className={filter === f ? "active" : ""} onClick={() => setFilter(f)}>
            {f === "" ? "Все" : f}
          </button>
        ))}
      </div>
      {showReminders && (
        <RemindersPanel cards={cards} onChanged={() => setReminderVersion((v) => v + 1)} />
      )}
      {!selected && (
        <EscalationsQueue
          items={escalations}
          onResolved={(id) => setEscalations((prev) => prev.filter((e) => e.id !== id))}
        />
      )}
      {!selected ? (
        <div className="card">
          <h2>Мои пациенты ({cards.length})</h2>
          {cards.length === 0 && <p style={{ color: "#5a6478" }}>Нет пациентов в выбранном фильтре.</p>}
          <div className="table-wrap">
            <table className="patients">
              <thead>
                <tr>
                  <th>Пациент</th>
                  <th>Триаж</th>
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
                    <td>{c.last_triage ? <span className={`badge ${c.last_triage}`}>{c.last_triage}</span> : "—"}</td>
                    <td>{c.last_score ?? "—"}</td>
                    <td>{c.open_escalations}</td>
                    <td>
                      <button
                        className="ghost"
                        onClick={() => {
                          window.location.hash = "patient=" + c.patient_id;
                        }}
                      >
                        Открыть
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <ReportView
          report={report}
          error={reportError}
          onBack={() => { window.location.hash = ""; }}
          onRefresh={() => setReportVersion((v) => v + 1)}
        />
      )}
    </>
  );
}

function ReportView({ report, error, onBack, onRefresh }: {
  report: Report | null; error: string; onBack: () => void; onRefresh: () => void;
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const [scaleSaved, setScaleSaved] = useState<number | null>(null);
  const [scaleError, setScaleError] = useState("");
  const [aeText, setAeText] = useState("");
  const [aeSeverity, setAeSeverity] = useState("mild");
  const [aeDate, setAeDate] = useState(new Date().toISOString().slice(0, 10));
  const [aeDrug, setAeDrug] = useState("");
  const [aeAction, setAeAction] = useState("monitoring");
  const [aeSaved, setAeSaved] = useState<number | null>(null);
  const [aeError, setAeError] = useState("");
  const [aeList, setAeList] = useState<Report["adverse_events"]>([]);
  const [chatMsgs, setChatMsgs] = useState<{ id: string; sender: string; content: string; created_at: string }[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [chatErr, setChatErr] = useState("");
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
    if (!report) return;
    let cancelled = false;
    api.clinicianChatList(report.patient_id)
      .then((r) => { if (!cancelled) setChatMsgs(r.items); })
      .catch(() => { if (!cancelled) setChatMsgs([]); });
    return () => { cancelled = true; };
  }, [report]);

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
    if (!report || picked.length === 0) return;
    setScaleError("");
    try {
      const res = await api.setPrescribedScales(report.patient_id, picked);
      setScaleSaved(Date.now());
      setPicked(res.scales);
    } catch (e: unknown) {
      setScaleError(e instanceof Error ? e.message : String(e));
    }
  };

  const submitAssessment = async () => {
    if (!report || !assessCode) return;
    const scale = report.available_scales.find((s) => s.code === assessCode);
    if (!scale) return;
    setAssessErr("");
    try {
      const answers = Object.fromEntries(
        scale.items.map((i) => [i.key, assessAnswers[i.key] ?? 0]),
      );
      setAssessResult(await api.createClinicianAssessment(report.patient_id, scale.code, answers));
      onRefresh();
    } catch (e: unknown) {
      setAssessErr(e instanceof Error ? e.message : String(e));
    }
  };

  const addAdverseEvent = async () => {
    if (!report || !aeText.trim()) return;
    setAeError("");
    try {
      const ev = await api.createAdverseEvent(report.patient_id, {
        event_text: aeText.trim(), severity: aeSeverity, onset_date: aeDate,
        suspected_drug: aeDrug.trim() || undefined, action_taken: aeAction,
      });
      setAeList([ev, ...aeList]);
      setAeText(""); setAeDrug(""); setAeSaved(Date.now());
    } catch (e: unknown) {
      setAeError(e instanceof Error ? e.message : String(e));
    }
  };

  const sendChat = async () => {
    if (!report || !chatInput.trim()) return;
    setChatErr("");
    try {
      const m = await api.clinicianChatPost(report.patient_id, chatInput.trim());
      setChatMsgs([...chatMsgs, m]);
      setChatInput("");
    } catch (e: unknown) {
      setChatErr(e instanceof Error ? e.message : String(e));
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
  const assessScale = report.available_scales.find((s) => s.code === assessCode);
  // Итог в summary свёрнутой карточки: свежая оценка из формы или последняя сохранённая.
  const assessFmt = (score: number, sev: string, at: string) =>
    `${score} · ${sev} · ${new Date(at).toLocaleDateString("ru-RU")}`;
  const assessSummary = assessResult
    ? assessFmt(assessResult.total_score, assessResult.severity, assessResult.computed_at)
    : report.last_assessment
      ? `последняя ${assessFmt(report.last_assessment.total_score, report.last_assessment.severity, report.last_assessment.computed_at)}`
      : "";
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

      <ContactsBlock patientId={report.patient_id} hasConsent={report.contacts !== null} />

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <Collapsible storageKey="cc.clinician.screening-scales" summary="Шкалы скрининга">
          <table className="scale-table" style={{ fontSize: 14 }}>
            <tbody>
              {report.available_scales.filter((s) => s.audience === "PATIENT").map((s) => (
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
            <button onClick={saveScale} disabled={picked.length === 0}>Назначить</button>
            {scaleSaved && <span style={{ color: "#2e7d32", fontSize: 13 }}>Сохранено</span>}
          </div>
        </Collapsible>
      </div>
      {scaleError && <p className="error">{scaleError}</p>}

      {report.available_scales.some((s) => s.audience === "CLINICIAN") && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <Collapsible
            storageKey="cc.clinician.assessment"
            summary={
              <>
                Оценка врачом: {assessScale?.code ?? "—"}
                {assessSummary && (
                  <span style={{ color: "#5a6478", fontSize: 13, fontWeight: 400 }}>
                    {" "}· {assessSummary}
                  </span>
                )}
              </>
            }
          >
            <select
              aria-label="Шкала врачебной оценки"
              value={assessCode}
              onChange={(e) => {
                setAssessCode(e.target.value);
                setAssessAnswers({});
                setAssessResult(null);
              }}
            >
              {report.available_scales.filter((s) => s.audience === "CLINICIAN").map((s) => (
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
              <button onClick={submitAssessment} disabled={!assessScale}>Сохранить оценку</button>
              {assessResult && (
                <span style={{ fontSize: 13 }}>
                  {assessResult.total_score} · {assessResult.severity} · {assessResult.triage}
                </span>
              )}
            </div>
            {assessErr && <p className="error">{assessErr}</p>}
          </Collapsible>
        </div>
      )}

      {report.checkin.recent.length > 0 && (
        <div className="card">
          <b>Чек-ины</b>{" "}
          <span style={{ color: "#5a6478", fontSize: 12 }}>
            правила {report.checkin.rules_version ?? "—"}
          </span>
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
        </div>
      )}
      {report.open_escalations.length > 0 && (
        <div className="alert-danger">
          <b>Открытые эскалации:</b>{" "}
          {report.open_escalations.map((e) => `${e.kind}${e.notified_at ? ` (${e.notified_at.slice(0, 10)})` : ""}`).join(", ")}
        </div>
      )}

      {report.routing && report.routing.category !== "NONE" && (
        <div className={report.routing.category === "REQUIRED" ? "alert-danger" : "alert-warn"}>
          <b>Маршрутизация: {report.routing.label} — направление к {report.routing.target}</b>
          <div style={{ fontSize: 13, marginTop: 4 }}>{report.routing.reason}</div>
        </div>
      )}
      {report.routing && report.routing.category === "NONE" && (
        <div className="row" style={{ color: "#5a6478", fontSize: 13 }}>
          Маршрутизация: не требуется — динамическое наблюдение
        </div>
      )}

      {last && (
        <div className="row">
          <span className={`badge ${last.triage}`}>{last.triage}</span>
          <span>
            Последний скрининг: балл {last.total_score} · {last.severity} · {last.computed_at.slice(0, 10)}
          </span>
        </div>
      )}

      <h3>Приверженность лечению (задача кейса 7, 14 дней)</h3>
      {report.adherence.pct !== null ? (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
            <div style={{ flex: 1, maxWidth: 320, height: 18, background: "#eef0f4", borderRadius: 9, overflow: "hidden" }}>
              <div style={{ width: `${Math.round(report.adherence.pct)}%`, height: "100%",
                background: report.adherence.pct >= 80 ? "#2e7d32" : report.adherence.pct >= 50 ? "#c9a227" : "#b3261e" }} />
            </div>
            <b>{report.adherence.pct}%</b>
            <span style={{ color: "#5a6478", fontSize: 13 }}>
              принят {report.adherence.taken} · частично {report.adherence.partial} · пропущен {report.adherence.missed}
            </span>
          </div>
          {report.adherence.records.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Дата</th><th>Препарат</th><th>Статус</th><th>Заметка</th></tr></thead>
                <tbody>
                  {report.adherence.records.map((r) => (
                    <tr key={r.id}>
                      <td>{r.created_at.slice(0, 10)}</td>
                      <td>{r.medication ?? "—"}</td>
                      <td>{r.status}</td>
                      <td>{r.note ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      ) : (
        <p style={{ color: "#5a6478", fontSize: 13 }}>Записей о приверженности за 14 дней нет.</p>
      )}

      <h3>Нежелательные явления (задача кейса 3)</h3>
      {aeList.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Дата</th><th>Событие</th><th>Тяжесть</th><th>Препарат</th><th>Действие</th></tr>
            </thead>
            <tbody>
              {aeList.map((e) => (
                <tr key={e.id}>
                  <td>{e.onset_date}</td>
                  <td>{e.event_text}</td>
                  <td><span className={`badge ${e.severity === "severe" ? "RED" : e.severity === "moderate" ? "YELLOW" : "GREEN"}`}>{e.severity}</span></td>
                  <td>{e.suspected_drug ?? "—"}</td>
                  <td>{e.action_taken ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p style={{ color: "#5a6478", fontSize: 13 }}>Нежелательные явления не зарегистрированы.</p>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 6, maxWidth: 560, marginTop: 8 }}>
        <input placeholder="Описание нежелательного явления" value={aeText} onChange={(e) => setAeText(e.target.value)} />
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <select aria-label="Тяжесть НЯ" value={aeSeverity} onChange={(e) => setAeSeverity(e.target.value)}>
            <option value="mild">лёгкое</option><option value="moderate">умеренное</option><option value="severe">тяжёлое</option>
          </select>
          <input type="date" value={aeDate} onChange={(e) => setAeDate(e.target.value)} style={{ width: "auto" }} />
          <input placeholder="Препарат (напр. Авиандр)" value={aeDrug} onChange={(e) => setAeDrug(e.target.value)} />
          <select aria-label="Действие при НЯ" value={aeAction} onChange={(e) => setAeAction(e.target.value)}>
            <option value="none">нет действия</option><option value="monitoring">наблюдение</option>
            <option value="dose_reduced">снижение дозы</option><option value="stopped">отмена</option>
            <option value="referred">направление</option>
          </select>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button onClick={addAdverseEvent} disabled={!aeText.trim()}>Зарегистрировать НЯ</button>
          {aeSaved && <span style={{ color: "#2e7d32", fontSize: 13 }}>Сохранено</span>}
        </div>
      </div>
      {aeError && <p className="error">{aeError}</p>}

      <h3>Переписка с пациентом (задача кейса 6)</h3>
      <div style={{ maxHeight: 280, overflowY: "auto", marginBottom: 8 }}>
        {chatMsgs.length > 0 ? chatMsgs.map((m) => (
          <div key={m.id} className={`msg ${m.sender === "SYSTEM" ? "system" : ""}`}>
            <b>{m.sender === "CLINICIAN" ? "Врач: " : m.sender === "SYSTEM" ? "Помощник: " : "Пациент: "}</b>
            {m.content}
          </div>
        )) : <p style={{ color: "#5a6478", fontSize: 13 }}>Переписки пока нет.</p>}
      </div>
      <div className="row">
        <input placeholder="Сообщение пациенту" value={chatInput}
          onChange={(e) => setChatInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && sendChat()} />
        <button onClick={sendChat} disabled={!chatInput.trim()}>Отправить</button>
      </div>
      {chatErr && <p className="error">{chatErr}</p>}

      <h3>Приёмы (запись беседы врач-пациент)</h3>
      <div style={{ display: "flex", flexDirection: "column", gap: 6, maxWidth: 560, marginTop: 8 }}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <input type="date" value={visitDate} onChange={(e) => setVisitDate(e.target.value)} />
          <select aria-label="Тип приёма" value={visitType} onChange={(e) => setVisitType(e.target.value as "in_person" | "online" | "phone")}>
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
      <EmiasBlock patientId={report.patient_id} />
      <MedicationsBlock patientId={report.patient_id} />
      <HistoryBlock patientId={report.patient_id} />

      <h3>Импорт истории болезни (метрика кейса 2)</h3>
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

      <h3>Динамика (90 дней)</h3>
      <TrendChart timeline={{ runs: report.history, trend: report.trend }} />

      <h3>История скринингов</h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Дата</th>
              <th>Балл</th>
              <th>Severity</th>
              <th>Триаж</th>
            </tr>
          </thead>
          <tbody>
            {report.history.map((h, i) => (
              <tr key={i}>
                <td>{h.computed_at.slice(0, 10)}</td>
                <td>{h.total_score}</td>
                <td>{h.severity}</td>
                <td>
                  <span className={`badge ${h.triage}`}>{h.triage}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

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

// FR-25: блок «ЕМИАС» в карточке врача — только чтение (связка, назначения, приёмы).
function EmiasBlock({ patientId }: { patientId: string }) {
  const [data, setData] = useState<EmiasSummary | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let cancelled = false;
    setErr("");
    setData(null);
    api.clinicianEmias(patientId)
      .then((r) => { if (!cancelled) setData(r); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : "нет данных"); });
    return () => { cancelled = true; };
  }, [patientId]);

  if (err) {
    return (
      <div>
        <h3>ЕМИАС</h3>
        <p style={{ color: "#5a6478", fontSize: 13 }}>Данные недоступны: {err}</p>
      </div>
    );
  }
  if (!data) return null;
  if (!data.linked) {
    return (
      <div>
        <h3>ЕМИАС</h3>
        <p style={{ color: "#5a6478", fontSize: 13 }}>Пациент не связан с ЕМИАС.</p>
      </div>
    );
  }
  return (
    <div>
      <h3>ЕМИАС</h3>
      <p style={{ color: "#5a6478", fontSize: 13 }}>
        приёмов: {data.encounters} ·
        последняя синхронизация: {data.last_sync ? data.last_sync.slice(0, 16).replace("T", " ") : "—"}
        {data.enabled ? "" : " · интеграция отключена (показаны сохранённые данные)"}
      </p>
      {/* назначения (включая импортированные) — в блоке «Назначения» (FR-23) */}
    </div>
  );
}

// FR-22: контакты пациента. Полный телефон отдаёт только endpoint контактов
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
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  // Защита от гонки: ответ устаревшего запроса не перезаписывает свежий список.
  const reqRef = useRef(0);

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
        notes: notes.trim() || undefined,
      });
      setTitle("");
      setDosage("");
      setNotes("");
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

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <h3>Назначения (FR-23)</h3>
      {items.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Препарат</th><th>Дозировка</th><th>С</th><th>По</th><th>Статус</th><th>Заметка</th><th></th></tr>
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
        <input placeholder="Препарат" value={title} onChange={(e) => setTitle(e.target.value)} style={{ maxWidth: 200 }} />
        <input placeholder="Дозировка" value={dosage} onChange={(e) => setDosage(e.target.value)} style={{ maxWidth: 150 }} />
        <input type="date" aria-label="Дата начала" value={startedOn} onChange={(e) => setStartedOn(e.target.value)} />
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

function RemindersPanel({ cards, onChanged }: { cards: PatientCard[]; onChanged: () => void }) {
  const [data, setData] = useState<RemindersResponse>({ items: [], overdue: 0 });
  const [patientId, setPatientId] = useState("");
  const [kind, setKind] = useState<ClinicianReminder["kind"]>("call");
  const [note, setNote] = useState("");
  // datetime-local ждёт локальное время без зоны: YYYY-MM-DDTHH:MM.
  const [due, setDue] = useState(() => {
    const d = new Date(Date.now() + 3600_000);
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = () => {
    api
      .clinicianReminders()
      .then((r) => {
        setData(r);
        setErr("");
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  };

  useEffect(() => {
    reload();
  }, []);

  useEffect(() => {
    // Список пациентов грузится асинхронно — подставляем первого, когда он появился.
    if (!patientId && cards.length > 0) setPatientId(cards[0].patient_id);
  }, [cards, patientId]);

  const create = async () => {
    if (!patientId || !due) {
      setErr("Выберите пациента и срок");
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
      reload();
      onChanged();
    } catch (e: unknown) {
      setErr(e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn: (id: string) => Promise<unknown>, id: string) => {
    try {
      await fn(id);
      reload();
      onChanged();
    } catch (e: unknown) {
      setErr(e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="card">
      <h2>Напоминания {data.overdue > 0 && <span className="badge RED">{data.overdue} просрочено</span>}</h2>
      <div className="row" style={{ alignItems: "flex-end", gap: 8, flexWrap: "wrap" }}>
        <label>
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
          <input
            value={note}
            maxLength={2000}
            onChange={(e) => setNote(e.target.value)}
            placeholder="например, уточнить дозу"
          />
        </label>
        <button disabled={busy} onClick={create}>
          Добавить
        </button>
      </div>
      {err && <p className="error">{err}</p>}
      {data.items.length === 0 && <p style={{ color: "#5a6478" }}>Открытых напоминаний нет.</p>}
      {REMINDER_GROUPS.map((group) => {
        const items = data.items.filter((r) => r.due_state === group.key);
        if (items.length === 0) return null;
        return (
          <div key={group.key} style={{ marginTop: 12 }}>
            <h3 style={{ fontSize: 15, margin: "8px 0" }}>
              {group.label} ({items.length})
            </h3>
            {items.map((r) => (
              <div
                key={r.id}
                className="row"
                style={{ alignItems: "center", gap: 8, flexWrap: "wrap", padding: "4px 0" }}
              >
                <a
                  href={"#patient=" + r.patient_id}
                  style={{ color: "#2f6fed", textDecoration: "none", fontWeight: 600 }}
                >
                  {r.patient_fio}
                </a>
                <span>{r.kind_label}</span>
                <small style={{ color: "#5a6478" }}>
                  {new Date(r.due_at).toLocaleString("ru-RU")}
                  {group.key === "overdue" && r.overdue_minutes > 0
                    ? ` · просрочено ${Math.floor(r.overdue_minutes / 60)} ч ${r.overdue_minutes % 60} мин`
                    : ""}
                </small>
                {r.note && <small style={{ color: "#5a6478" }}>— {r.note}</small>}
                <button className="ghost" onClick={() => act(api.doneReminder, r.id)}>
                  Готово
                </button>
                <button className="ghost" onClick={() => act(api.cancelReminder, r.id)}>
                  Отменить
                </button>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}