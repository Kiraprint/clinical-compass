import { useEffect, useState } from "react";
import { api, PatientCard, Report } from "../api";
import TrendChart from "../TrendChart";
import AssistantTab from "./AssistantTab";

const TRIAGE_FILTERS = ["", "GREEN", "YELLOW", "RED"] as const;

export default function ClinicianView() {
  const [section, setSection] = useState<"patients" | "assistant">("patients");
  const [filter, setFilter] = useState<(typeof TRIAGE_FILTERS)[number]>("");
  const [cards, setCards] = useState<PatientCard[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [reportError, setReportError] = useState("");

  const load = async (f: string) => {
    const data = await api.clinicianPatients(f || undefined);
    setCards(data.items);
  };

  useEffect(() => {
    load(filter);
  }, [filter]);

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
  }, [selected]);

  if (section === "assistant") {
    return (
      <>
        <div className="tabs">
          <button onClick={() => setSection("patients")}>Пациенты</button>
          <button className="active" onClick={() => setSection("assistant")}>
            Ассистент
          </button>
        </div>
        <AssistantTab />
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
        <ReportView report={report} error={reportError} onBack={() => { window.location.hash = ""; }} />
      )}
    </>
  );
}

function ReportView({ report, error, onBack }: { report: Report | null; error: string; onBack: () => void }) {
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
  useEffect(() => {
    if (report) { setPicked(report.prescribed_scales ?? []); setAeList(report.adverse_events ?? []); }
  }, [report]);

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
            {report.available_scales.map((s) => (
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
          <select value={aeSeverity} onChange={(e) => setAeSeverity(e.target.value)}>
            <option value="mild">лёгкое</option><option value="moderate">умеренное</option><option value="severe">тяжёлое</option>
          </select>
          <input type="date" value={aeDate} onChange={(e) => setAeDate(e.target.value)} style={{ width: "auto" }} />
          <input placeholder="Препарат (напр. Авиандр)" value={aeDrug} onChange={(e) => setAeDrug(e.target.value)} />
          <select value={aeAction} onChange={(e) => setAeAction(e.target.value)}>
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