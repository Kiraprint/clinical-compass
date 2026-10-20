import { useEffect, useState } from "react";
import { api, Assessment, ImportHistoryResult, PatientCard, Report, Visit } from "../api";
import TrendChart from "../TrendChart";
import AssistantTab from "./AssistantTab";
import DrugLibraryTab from "./DrugLibraryTab";

const TRIAGE_FILTERS = ["", "GREEN", "YELLOW", "RED"] as const;

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
      </div>
      <div className="tabs">
        {TRIAGE_FILTERS.map((f) => (
          <button key={f} className={filter === f ? "active" : ""} onClick={() => setFilter(f)}>
            {f === "" ? "Все" : f}
          </button>
        ))}
      </div>
      {!selected ? (
        <div className="card">
          <h2>Мои пациенты ({cards.length})</h2>
          {cards.length === 0 && <p style={{ color: "#5a6478" }}>Нет пациентов в выбранном фильтре.</p>}
          <div className="table-wrap">
            <table>
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

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <h3 style={{ margin: 0 }}>Шкалы скрининга</h3>
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
      </div>
      {scaleError && <p className="error">{scaleError}</p>}

      {report.available_scales.some((s) => s.audience === "CLINICIAN") && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <h3 style={{ margin: 0 }}>Оценка врачом</h3>
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
            <label key={i.key} style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: 14 }}>
              <span>{i.text}</span>
              <input
                type="number"
                min={0}
                max={i.max_value}
                value={assessAnswers[i.key] ?? 0}
                onChange={(e) =>
                  setAssessAnswers((cur) => ({
                    ...cur,
                    [i.key]: Math.max(0, Math.min(i.max_value, Number(e.target.value) || 0)),
                  }))
                }
                style={{ width: 64 }}
              />
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
          {d.mood ? ` (настроение ${d.mood}/10)` : ""}
        </div>
      ))}
    </div>
  );
}