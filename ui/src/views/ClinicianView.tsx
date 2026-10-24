import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, AdverseSeverity, ApiError, Assessment, ClinicianAdverseEvent, ClinicianReminder, Drug, EmiasSummary, Escalation, HistoryType, ImportHistoryResult, PatientCard, PatientChatMessage, PatientContacts, PatientHistoryItem, PatientMedication, RemindersResponse, Report, Visit } from "../api";
import TrendChart from "../TrendChart";
import ChatsView from "./ChatsView";
import DrugLibraryTab from "./DrugLibraryTab";

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
  // Карточка видна всегда: без открытых сигналов она даёт доступ к истории разбора.
  return (
    <div className="card">
      <h2>Очередь эскалаций ({items.length})</h2>
      <p style={{ color: "#5a6478", fontSize: 13 }}>
        Открытые сигналы чек-инов и скринингов, включая пациентов без закреплённого врача.
      </p>
      <p style={{ marginBottom: 12 }}>
        <button onClick={() => setShowClosed((v) => !v)}>
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
    </div>
  );
}

export default function ClinicianView() {
  const [section, setSection] = useState<"patients" | "chats" | "library">("patients");
  // FR-29: выбранный пациент на экране «Чаты» (deep-link #chats=patient:<id>).
  const [chatPatient, setChatPatient] = useState<string | null>(null);
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
  // FR-28: НЯ, сообщённые пациентами и ждущие проверки врача.
  const [pendingAe, setPendingAe] = useState<ClinicianAdverseEvent[]>([]);
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


  useEffect(() => {
    if (selected) return;
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

  // FR-29: навигация врача — Пациенты · Чаты · Библиотека · Напоминания.
  const nav = (
    <div className="tabs top-tabs">
      <button
        className={section === "patients" && !showReminders ? "active" : ""}
        onClick={() => {
          setSection("patients");
          setShowReminders(false);
        }}
      >
        Пациенты
      </button>
      <button className={section === "chats" ? "active" : ""} onClick={() => setSection("chats")}>
        Чаты
      </button>
      <button
        className={section === "library" ? "active" : ""}
        onClick={() => setSection("library")}
      >
        Библиотека
      </button>
      <button
        className={`push-right${section === "patients" && showReminders ? " active" : ""}`}
        onClick={() => {
          setSection("patients");
          setShowReminders((v) => !v);
        }}
      >
        Напоминания
        {overdueCount > 0 && <span className="count-badge">{overdueCount}</span>}
      </button>
    </div>
  );

  if (section === "chats") {
    return (
      <>
        {nav}
        <ChatsView
          patientId={chatPatient}
          onSelectPatient={(id) => {
            setChatPatient(id);
            if (id) window.location.hash = `chats=patient:${id}`;
          }}
        />
      </>
    );
  }

  if (section === "library") {
    return (
      <>
        {nav}
        <DrugLibraryTab />
      </>
    );
  }

  return (
    <>
      {nav}
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
      {!selected && pendingAe.length > 0 && (
        <div className="alert-warn" style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
          <strong>НЯ на проверке: {pendingAe.length}</strong>
          <span style={{ fontSize: 13 }}>
            {pendingAe.slice(0, 3).map((e) => e.patient_fio ?? "Пациент").join(", ")}
            {pendingAe.length > 3 ? " …" : ""} — откройте карточку пациента.
          </span>
        </div>
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
          <p className="muted small">
            Сначала красные: выше — больше открытых эскалаций и более свежая оценка.
          </p>
          {cards.length === 0 && <p className="muted">Нет пациентов в выбранном фильтре.</p>}
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
          onOpenLibrary={() => { setSelected(null); setSection("library"); }}
          onOpenChats={(pid) => { window.location.hash = `chats=patient:${pid}`; }}
        />
      )}
    </>
  );
}

function ReportView({ report, error, onBack, onRefresh, onOpenLibrary, onOpenChats }: {
  report: Report | null; error: string; onBack: () => void; onRefresh: () => void;
  onOpenLibrary: () => void; onOpenChats: (patientId: string) => void;
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const [scaleSaved, setScaleSaved] = useState<number | null>(null);
  const [scaleError, setScaleError] = useState("");
  const [aeList, setAeList] = useState<Report["adverse_events"]>([]);
  // FR-28: справочник ГРЛС для связи НЯ с препаратом (доступ врачу).
  const [drugs, setDrugs] = useState<Drug[]>([]);
  const [chatMsgs, setChatMsgs] = useState<PatientChatMessage[]>([]);
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
  const pendingAeCount = aeList.filter((e) => e.status === "pending").length;
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
      <Collapsible storageKey="cc.clinician.reminders" summary="Напоминания">
        <PatientReminders patientId={report.patient_id} />
      </Collapsible>

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

      <h3>Приверженность лечению (FR-27, 14 дней)</h3>
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
            <b>Общий процент: {report.adherence.pct}%</b>{" "}
            <span style={{ color: "#5a6478" }}>
              — это сумма (принято + доля каждого «частично»: 1/4 = 0,25, 1/2 = 0,5,
              3/4 = 0,75, 1 из 2 = 0,5, 2 из 3 ≈ 0,67; «меньше/изменил сам» = 0,5)
              по назначениям с заданным режимом, делённая на сумму ожидаемых приёмов;
              не среднее процентов по препаратам. «Не назначено сегодня» в расчёт не входит.
              {report.adherence.excluded_count > 0
                ? ` Вне расчёта ${report.adherence.excluded_count} отметок без привязки к назначению или у назначений без режима.`
                : ""}
            </span>
          </p>
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
          {report.adherence.total > 0 && ` Отметок за 14 дней: ${report.adherence.total}, все вне расчёта.`}
        </p>
      )}

      <h3>Нежелательные явления</h3>
      {/* FR-28: единый блок — проверка сообщений пациента и ручное сообщение врача. */}
      <div style={{ display: "flex", flexDirection: "column", gap: 10, marginBottom: 12 }}>
        {pendingAeCount > 0 && (
          <>
            <strong style={{ fontSize: 14 }}>На проверке: {pendingAeCount}</strong>
            {aeList
              .filter((e) => e.status === "pending")
              .map((e) => (
                <VerifyAdverseEvent
                  key={e.id}
                  event={e}
                  medications={report.adherence.medications}
                  drugs={drugs}
                  onOpenLibrary={onOpenLibrary}
                  onVerified={(updated) => {
                    setAeList((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
                    onRefresh();
                  }}
                />
              ))}
          </>
        )}
        <ReportAdverseEvent
          patientId={report.patient_id}
          onCreated={(created) => {
            setAeList((prev) => [created, ...prev]);
            onRefresh();
          }}
        />
      </div>
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
                  <td><span className={`badge ${e.severity === "severe" ? "RED" : e.severity === "moderate" ? "YELLOW" : "GREEN"}`}>{e.severity}</span></td>
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
      <h3>Переписка с пациентом</h3>
      <div className="chat-mini">
        {chatMsgs.length > 0 ? chatMsgs.slice(-3).map((m) => (
          <div key={m.id} className={`msg ${m.sender === "SYSTEM" ? "system" : ""}`}>
            <b>{m.sender === "CLINICIAN" ? "Врач: " : m.sender === "SYSTEM" ? "Помощник: " : "Пациент: "}</b>
            {m.content}
          </div>
        )) : <p className="muted small">Переписки пока нет.</p>}
      </div>
      <div className="row">
        <button className="ghost" onClick={() => onOpenChats(report.patient_id)}>
          Открыть в чатах
        </button>
      </div>

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