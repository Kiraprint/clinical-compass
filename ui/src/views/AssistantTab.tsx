import { useEffect, useMemo, useState, type ChangeEvent } from "react";
import { api, AssistantAnswer, KB_DESIGN_LABELS, KbWork, PatientCard } from "../api";

export default function AssistantTab() {
  const [query, setQuery] = useState("");
  const [answer, setAnswer] = useState<AssistantAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [points, setPoints] = useState<number | null>(null);
  const [patients, setPatients] = useState<PatientCard[]>([]);
  const [mentionOpen, setMentionOpen] = useState(false);
  const [mentionQ, setMentionQ] = useState("");
  const [patientId, setPatientId] = useState<string | null>(null);
  const [patientLabel, setPatientLabel] = useState("");
  const [sources, setSources] = useState<KbWork[]>([]);
  const [excludedIds, setExcludedIds] = useState<string[]>([]);
  const [sourcesLoaded, setSourcesLoaded] = useState(false);
  const [sourcesError, setSourcesError] = useState("");
  const [savingSource, setSavingSource] = useState<string | null>(null);

  useEffect(() => {
    api.assistantStatus().then((s) => setPoints(s.points)).catch(() => setPoints(null));
    api.clinicianPatients().then((r) => setPatients(r.items)).catch(() => setPatients([]));
    api
      .assistantSources()
      .then((r) => {
        setSources(r.sources);
        setExcludedIds(r.excluded_guideline_ids);
      })
      .catch((e: unknown) => setSourcesError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSourcesLoaded(true));
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
      setAnswer(await api.assistantAsk(query.trim(), 5, patientId ?? undefined));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // Персональное исключение: сервер возвращает полный список скрытых id — им и
  // синхронизируем состояние, чтобы не расходиться с другими вкладками.
  const toggleSource = async (guidelineId: string, excluded: boolean) => {
    setSavingSource(guidelineId);
    setSourcesError("");
    try {
      const r = await api.assistantSetSourceExcluded(guidelineId, excluded);
      setExcludedIds(r.excluded_guideline_ids);
    } catch (e: unknown) {
      setSourcesError(e instanceof Error ? e.message : String(e));
    } finally {
      setSavingSource(null);
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

      <details style={{ marginTop: 16 }}>
        <summary style={{ cursor: "pointer", fontSize: 14, fontWeight: 600, color: "#2f6fed" }}>
          Источники ({sources.length})
        </summary>
        <p style={{ fontSize: 12, color: "#8a94a8", margin: "6px 0" }}>
          Одобренные источники базы знаний. Скрытый источник остаётся в общей базе, но не
          попадает в вашу выдачу.
        </p>
        {!sourcesLoaded && <p style={{ color: "#5a6478", fontSize: 13 }}>Загрузка…</p>}
        {sourcesError && (
          <p className="error">Не удалось загрузить источники: {sourcesError}</p>
        )}
        {sourcesLoaded && sources.length === 0 && !sourcesError && (
          <p style={{ color: "#5a6478", fontSize: 13 }}>Одобренных источников пока нет.</p>
        )}
        {sources.map((s) => {
          const hidden = excludedIds.includes(s.guideline_id);
          const link = s.fulltext_url ?? s.url;
          const design = s.design?.design ?? "";
          return (
            <div key={s.id} style={{ borderTop: "1px solid #e2e6ee", padding: "8px 0" }}>
              <div style={{ fontWeight: 600, fontSize: 14 }}>{s.title}</div>
              <div className="row" style={{ fontSize: 12, color: "#5a6478" }}>
                <span>{s.kind === "guideline" ? "Гайдлайн ВОЗ" : "Статья"}</span>
                {s.year ? <span>{s.year}</span> : null}
                <span>{KB_DESIGN_LABELS[design] ?? (design || "дизайн не определён")}</span>
                {link && (
                  <a href={link} target="_blank" rel="noreferrer">
                    Открыть источник
                  </a>
                )}
              </div>
              <label style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 4 }}>
                <input
                  type="checkbox"
                  checked={hidden}
                  disabled={savingSource === s.guideline_id}
                  onChange={(e) => void toggleSource(s.guideline_id, e.target.checked)}
                />
                Скрыть из моей выдачи
              </label>
            </div>
          );
        })}
      </details>
    </div>
  );
}
