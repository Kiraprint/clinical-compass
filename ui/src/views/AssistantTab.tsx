import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  api,
  AssistantThread,
  AssistantThreadDetail,
  KB_DESIGN_LABELS,
  KbWork,
  PatientCard,
} from "../api";

const chipStyle = (active: boolean): CSSProperties => ({
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  background: active ? "#eef2ff" : "#f6f7fb",
  border: `1px solid ${active ? "#c7d2fe" : "#d4d8e3"}`,
  borderRadius: 12,
  padding: "2px 10px",
  fontSize: 13,
  cursor: "pointer",
});

export default function AssistantTab() {
  // FR-21: сессии ассистента — треды с persistent-контекстом и пациентами.
  const [threads, setThreads] = useState<AssistantThread[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AssistantThreadDetail | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [renaming, setRenaming] = useState("");
  const [points, setPoints] = useState<number | null>(null);
  const [patients, setPatients] = useState<PatientCard[]>([]);
  const [sources, setSources] = useState<KbWork[]>([]);
  const [excludedIds, setExcludedIds] = useState<string[]>([]);
  const [sourcesLoaded, setSourcesLoaded] = useState(false);
  const [sourcesError, setSourcesError] = useState("");
  const [savingSource, setSavingSource] = useState<string | null>(null);

  // Номер последнего запроса детали треда: ответ старого запроса не должен
  // перезаписать открытый сейчас диалог.
  const selectSeq = useRef(0);

  const patientLabels = useMemo(() => {
    const map: Record<string, string> = {};
    for (const p of patients) map[p.patient_id] = p.fio || p.email;
    return map;
  }, [patients]);

  useEffect(() => {
    api.assistantStatus().then((s) => setPoints(s.points)).catch(() => setPoints(null));
    api.clinicianPatients().then((r) => setPatients(r.items)).catch(() => setPatients([]));
    api
      .assistantThreads()
      .then((r) => {
        setThreads(r.items);
        if (r.items.length > 0) void selectThread(r.items[0].id);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
    api
      .assistantSources()
      .then((r) => {
        setSources(r.sources);
        setExcludedIds(r.excluded_guideline_ids);
      })
      .catch((e: unknown) => setSourcesError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSourcesLoaded(true));
  }, []);

  const refreshThreads = async () => {
    const r = await api.assistantThreads();
    setThreads(r.items);
    return r.items;
  };

  async function selectThread(id: string) {
    const seq = ++selectSeq.current;
    setActiveId(id);
    setDetail(null);
    setError("");
    setNotice("");
    try {
      const loaded = await api.assistantThread(id);
      if (selectSeq.current !== seq) return; // ответ устаревшего запроса
      setDetail(loaded);
    } catch (e: unknown) {
      if (selectSeq.current !== seq) return;
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const createThread = async () => {
    setBusy(true);
    setError("");
    try {
      const thread = await api.assistantCreateThread();
      setThreads((t) => [thread, ...t]);
      await selectThread(thread.id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const renameThread = async () => {
    if (!detail) return;
    const title = renaming.trim();
    if (!title || title === detail.title) {
      setRenaming("");
      return;
    }
    setBusy(true);
    try {
      const updated = await api.assistantUpdateThread(detail.id, { title });
      setDetail((cur) =>
        cur && cur.id === detail.id ? { ...cur, ...updated } : cur,
      );
      await refreshThreads();
      setRenaming("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const deleteThread = async () => {
    if (!detail) return;
    if (!window.confirm(`Удалить диалог «${detail.title}» со всеми сообщениями?`)) return;
    setBusy(true);
    try {
      await api.assistantDeleteThread(detail.id);
      setDetail(null);
      setActiveId(null);
      const rest = await refreshThreads();
      if (rest.length > 0) await selectThread(rest[0].id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // Область пациентов треда: roster-пациенты чекбоксами; сервер дедуплицирует
  // и проверяет привязку (не roster → 403 PATIENT_NOT_IN_ROSTER).
  const togglePatient = async (patientId: string) => {
    if (!detail) return;
    const ids = detail.patient_ids.includes(patientId)
      ? detail.patient_ids.filter((id) => id !== patientId)
      : [...detail.patient_ids, patientId];
    setBusy(true);
    setError("");
    try {
      const updated = await api.assistantUpdateThread(detail.id, { patient_ids: ids });
      setDetail((cur) => (cur && cur.id === detail.id ? { ...cur, ...updated } : cur));
      await refreshThreads();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const send = async () => {
    if (!detail || !input.trim()) return;
    const targetId = detail.id;
    const seq = ++selectSeq.current;
    setBusy(true);
    setError("");
    setNotice("");
    const text = input.trim();
    try {
      const result = await api.assistantPostMessage(targetId, text);
      setInput("");
      if (result.skipped_patient_ids.length > 0) {
        setNotice(
          `Пропущены пациенты без доступа: ${result.skipped_patient_ids
            .map((id) => patientLabels[id] ?? id)
            .join(", ")}`,
        );
      }
      const fresh = await api.assistantThread(targetId);
      if (selectSeq.current === seq) setDetail(fresh); // врач не переключил тред
      await refreshThreads();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

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
        Диалоги с картами пациентов: в ответ попадают карты, закреплённые за тредом;
        история диалога сохраняется и видна, но поиск идёт по текущему вопросу.
        {points !== null && <> Корпус: {points} чанков.</>}
      </p>

      <div style={{ display: "flex", gap: 16, marginTop: 12, alignItems: "flex-start" }}>
        <aside style={{ width: 240, flexShrink: 0 }}>
          <button onClick={() => void createThread()} disabled={busy} style={{ width: "100%" }}>
            + Новый диалог
          </button>
          <div style={{ marginTop: 8 }}>
            {threads.map((t) => (
              <button
                key={t.id}
                onClick={() => void selectThread(t.id)}
                style={{
                  display: "block",
                  width: "100%",
                  textAlign: "left",
                  border: "none",
                  borderLeft: `3px solid ${t.id === activeId ? "#2f6fed" : "transparent"}`,
                  background: t.id === activeId ? "#eef2ff" : "transparent",
                  color: "#2a3350",
                  padding: "6px 8px",
                  cursor: "pointer",
                  fontSize: 13,
                }}
              >
                <div style={{ fontWeight: 600 }}>{t.title}</div>
                <div style={{ color: "#8a94a8", fontSize: 11 }}>
                  {t.message_count} сообщ. · {t.patient_ids.length} пациент(ов)
                </div>
              </button>
            ))}
            {threads.length === 0 && (
              <p style={{ color: "#8a94a8", fontSize: 12 }}>Диалогов пока нет.</p>
            )}
          </div>
        </aside>

        <section style={{ flex: 1, minWidth: 0 }}>
          {detail === null ? (
            <p style={{ color: "#5a6478", fontSize: 13 }}>
              Создайте диалог, чтобы задать вопрос по базе знаний.
            </p>
          ) : (
            <>
              <div className="row" style={{ alignItems: "center", gap: 8 }}>
                {renaming !== "" ? (
                  <>
                    <input
                      value={renaming}
                      onChange={(e) => setRenaming(e.target.value)}
                      maxLength={200}
                      style={{ flex: 1 }}
                    />
                    <button onClick={() => void renameThread()} disabled={busy}>
                      Сохранить
                    </button>
                    <button onClick={() => setRenaming("")}>Отмена</button>
                  </>
                ) : (
                  <>
                    <strong style={{ flex: 1 }}>{detail.title}</strong>
                    <button onClick={() => setRenaming(detail.title)} disabled={busy}>
                      Переименовать
                    </button>
                    <button onClick={() => void deleteThread()} disabled={busy}>
                      Удалить
                    </button>
                  </>
                )}
              </div>

              <div style={{ marginTop: 8 }}>
                <div style={{ fontSize: 12, color: "#5a6478", marginBottom: 4 }}>
                  Пациенты в контексте (roster):
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {patients.map((p) => {
                    const active = detail.patient_ids.includes(p.patient_id);
                    return (
                      <button
                        key={p.patient_id}
                        style={chipStyle(active)}
                        disabled={busy}
                        onClick={() => void togglePatient(p.patient_id)}
                        title={active ? "Убрать из контекста" : "Добавить в контекст"}
                      >
                        {p.fio || p.email}
                        {active ? " ✓" : ""}
                      </button>
                    );
                  })}
                  {patients.length === 0 && (
                    <span style={{ color: "#8a94a8", fontSize: 12 }}>
                      Нет закреплённых пациентов.
                    </span>
                  )}
                </div>
              </div>

              <div style={{ marginTop: 12, maxHeight: 420, overflow: "auto" }}>
                {detail.messages.length === 0 && (
                  <p style={{ color: "#8a94a8", fontSize: 13 }}>Сообщений пока нет.</p>
                )}
                {detail.messages.map((m) => (
                  <div
                    key={m.id}
                    style={{
                      margin: "8px 0",
                      padding: "8px 10px",
                      borderRadius: 8,
                      background: m.role === "CLINICIAN" ? "#f6f7fb" : "#ffffff",
                      border: "1px solid #e2e6ee",
                    }}
                  >
                    <div style={{ fontSize: 11, color: "#8a94a8" }}>
                      {m.role === "CLINICIAN" ? "Врач" : "Ассистент"}
                      {m.topic ? ` · ${m.topic}` : ""}
                    </div>
                    {m.role === "ASSISTANT" ? (
                      <pre className="assistant-answer" style={{ whiteSpace: "pre-wrap" }}>
                        {m.content}
                      </pre>
                    ) : (
                      <div style={{ whiteSpace: "pre-wrap" }}>{m.content}</div>
                    )}
                    {m.citations.length > 0 && (
                      <div className="assistant-sources">
                        <strong>Источники:</strong>
                        <ul>
                          {m.citations.map((c, i) => (
                            <li key={i}>
                              «{c.source_title}» · {c.topic}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                ))}
              </div>

              <label style={{ display: "block", marginTop: 8 }}>
                Вопрос
                <textarea
                  rows={2}
                  value={input}
                  maxLength={2000}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder="Например: какова динамика пациента из контекста и что говорит шкала PHQ-9?"
                />
              </label>
              <div className="row" style={{ marginTop: 8 }}>
                <button disabled={busy || !input.trim()} onClick={() => void send()}>
                  {busy ? "Ассистент думает…" : "Спросить"}
                </button>
              </div>
            </>
          )}

          {notice && <p style={{ color: "#9a6b00", fontSize: 13 }}>{notice}</p>}
          {error && <p className="error">{error}</p>}
        </section>
      </div>

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
