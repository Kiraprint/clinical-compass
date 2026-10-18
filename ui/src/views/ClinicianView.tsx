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

  const open = async (patientId: string) => {
    setSelected(patientId);
    setReportError("");
    try {
      setReport(await api.clinicianReport(patientId));
    } catch (e: unknown) {
      setReport(null);
      setReportError(e instanceof Error ? e.message : String(e));
    }
  };

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
                      {c.fio ?? c.email}
                      <br />
                      <small>{c.email}</small>
                    </td>
                    <td>{c.last_triage ? <span className={`badge ${c.last_triage}`}>{c.last_triage}</span> : "—"}</td>
                    <td>{c.last_score ?? "—"}</td>
                    <td>{c.open_escalations}</td>
                    <td>
                      <button className="ghost" onClick={() => open(c.patient_id)}>
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
        <ReportView report={report} error={reportError} onBack={() => setSelected(null)} />
      )}
    </>
  );
}

function ReportView({ report, error, onBack }: { report: Report | null; error: string; onBack: () => void }) {
  const [picked, setPicked] = useState<string[]>([]);
  const [scaleSaved, setScaleSaved] = useState<number | null>(null);
  const [scaleError, setScaleError] = useState("");
  useEffect(() => {
    if (report) setPicked(report.prescribed_scales ?? []);
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
      <h2>{report.fio ?? report.email}</h2>
      <p style={{ color: "#5a6478" }}>{report.email}</p>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <label style={{ margin: 0 }}>Шкалы скрининга</label>
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

      {last && (
        <div className="row">
          <span className={`badge ${last.triage}`}>{last.triage}</span>
          <span>
            Последний скрининг: балл {last.total_score} · {last.severity} · {last.computed_at.slice(0, 10)}
          </span>
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