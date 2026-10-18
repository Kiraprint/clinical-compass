import { useEffect, useMemo, useState, type ChangeEvent } from "react";
import { api, AssistantAnswer, PatientCard } from "../api";

const TOPICS = [
  { value: "", label: "Все" },
  { value: "ШКАЛЫ", label: "Шкалы" },
  { value: "ТРИАЖ", label: "Триаж" },
  { value: "РЕКОМЕНДАЦИИ", label: "Рекомендации" },
  { value: "ПРЕПАРАТЫ", label: "Препараты" },
] as const;

export default function AssistantTab() {
  const [query, setQuery] = useState("");
  const [topic, setTopic] = useState("");
  const [answer, setAnswer] = useState<AssistantAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [points, setPoints] = useState<number | null>(null);
  const [patients, setPatients] = useState<PatientCard[]>([]);
  const [mentionOpen, setMentionOpen] = useState(false);
  const [mentionQ, setMentionQ] = useState("");
  const [patientId, setPatientId] = useState<string | null>(null);
  const [patientLabel, setPatientLabel] = useState("");

  useEffect(() => {
    api.assistantStatus().then((s) => setPoints(s.points)).catch(() => setPoints(null));
    api.clinicianPatients().then((r) => setPatients(r.items)).catch(() => setPatients([]));
  }, []);

  // @-упоминание: фильтр по ротстеру врача (подстрока по ФИО/email).
  // Для ротстера демо-масштаба этого достаточно; tsvector/pg_trgm — YAGNI.
  const mentionMatches = useMemo(() => {
    if (!mentionOpen) return [];
    const q = mentionQ.toLowerCase();
    return patients.filter((p) => (p.fio || p.email).toLowerCase().includes(q)).slice(0, 8);
  }, [mentionOpen, mentionQ, patients]);

  const handleChange = (e: ChangeEvent<HTMLTextAreaElement>) => {
    const v = e.target.value;
    setQuery(v);
    const m = v.match(/@([^\s@]*)$/);
    if (m) {
      setMentionOpen(true);
      setMentionQ(m[1]);
    } else {
      setMentionOpen(false);
    }
    // Если тег выбранного пациента удалили из текста — открепляем контекст.
    if (patientLabel && !v.includes(`@${patientLabel}`)) {
      setPatientId(null);
      setPatientLabel("");
    }
  };

  const selectPatient = (p: PatientCard) => {
    const label = p.fio ?? p.email;
    setQuery((q) => q.replace(/@([^\s@]*)$/, `@${label} `));
    setPatientId(p.patient_id);
    setPatientLabel(label);
    setMentionOpen(false);
  };

  const clearPatient = () => {
    setPatientId(null);
    setPatientLabel("");
  };

  const ask = async () => {
    if (!query.trim()) return;
    setBusy(true);
    setError("");
    setAnswer(null);
    try {
      setAnswer(await api.assistantAsk(query.trim(), topic || undefined, 5, patientId ?? undefined));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <h2>Ассистент врача</h2>
      <p style={{ color: "#5a6478", fontSize: 13 }}>
        Справочный поиск по базе клинических рекомендаций и шкал. Упомяните пациента
        через «@», чтобы подгрузить его контекст.
        {points !== null && <> Корпус: {points} чанков.</>}
      </p>

      <div className="row">
        <label>
          Раздел
          <select value={topic} onChange={(e) => setTopic(e.target.value)}>
            {TOPICS.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {patientId !== null && (
        <div className="row" style={{ marginTop: 8 }}>
          <span
            className="patient-chip"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              background: "#eef2ff",
              border: "1px solid #c7d2fe",
              borderRadius: 12,
              padding: "2px 10px",
              fontSize: 13,
            }}
          >
            Контекст пациента: {patientLabel}
            <button
              onClick={clearPatient}
              style={{ border: "none", background: "transparent", cursor: "pointer", fontWeight: 700 }}
            >
              ✕
            </button>
          </span>
        </div>
      )}

      <label style={{ position: "relative" }}>
        Вопрос
        <textarea
          rows={2}
          value={query}
          onChange={handleChange}
          placeholder="Например: @Иван — какой порог тяжести у шкалы PHQ-9?"
        />
        {mentionOpen && mentionMatches.length > 0 && (
          <div
            className="mention-dropdown"
            style={{
              position: "absolute",
              left: 0,
              right: 0,
              top: "100%",
              background: "#fff",
              border: "1px solid #d4d8e3",
              borderRadius: 6,
              boxShadow: "0 4px 12px rgba(0,0,0,0.12)",
              zIndex: 10,
              maxHeight: 220,
              overflow: "auto",
            }}
          >
            {mentionMatches.map((p) => (
              <button
                key={p.patient_id}
                onClick={() => selectPatient(p)}
                style={{
                  display: "block",
                  width: "100%",
                  textAlign: "left",
                  border: "none",
                  background: "transparent",
                  color: "#2a3350",
                  padding: "6px 10px",
                  cursor: "pointer",
                  fontSize: 13,
                }}
              >
                {p.fio || p.email}
                {p.last_triage && <span style={{ color: "#5a6478", marginLeft: 8 }}>· {p.last_triage}</span>}
              </button>
            ))}
          </div>
        )}
      </label>

      <div className="row" style={{ marginTop: 10 }}>
        <button disabled={busy || !query.trim()} onClick={ask}>
          {busy ? "Поиск…" : "Спросить"}
        </button>
      </div>

      {error && <p className="error">{error}</p>}

      {answer && (
        <div style={{ marginTop: 16 }}>
          <pre className="assistant-answer">{answer.answer}</pre>
          {answer.citations.length > 0 && (
            <div className="assistant-sources">
              <strong>Источники:</strong>
              <ul>
                {answer.citations.map((c, i) => (
                  <li key={i}>
                    «{c.source_title}» · {c.topic}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
