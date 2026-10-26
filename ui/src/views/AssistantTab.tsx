import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  AssistantThread,
  AssistantThreadDetail,
  AssistantThreadMessage,
  KB_DESIGN_LABELS,
  KbWork,
  PatientCard,
} from "../api";

import { isSendShortcut } from "../keys";

// FR-32: чипы пациентов в контексте диалога. Класс .chip задаёт и цвет текста —
// раньше inline-фон перекрывал белую inherited-надпись от глобального button.

export default function AssistantTab({ patients }: { patients: PatientCard[] }) {
  // FR-21: сессии ассистента — треды с persistent-контекстом и пациентами.
  const [threads, setThreads] = useState<AssistantThread[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AssistantThreadDetail | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [pendingUser, setPendingUser] = useState("");
  const [streamThreadId, setStreamThreadId] = useState<string | null>(null);
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [renaming, setRenaming] = useState("");
  const [points, setPoints] = useState<number | null>(null);
  const [sources, setSources] = useState<KbWork[]>([]);
  const [excludedIds, setExcludedIds] = useState<string[]>([]);
  const [sourcesLoaded, setSourcesLoaded] = useState(false);
  const [sourcesError, setSourcesError] = useState("");
  const [savingSource, setSavingSource] = useState<string | null>(null);
  // FR-29: на узком экране список диалогов сворачивается в кнопку над диалогом.
  const [listOpen, setListOpen] = useState(false);

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

  // Кнопка «Обновить»: перечитывает список и открытый диалог (без перезагрузки).
  const reloadAll = async () => {
    setBusy(true);
    setError("");
    // Явное «Обновить» — запрос за свежим состоянием, мимо кэша GET.
    // Ассистент меняет только треды: остальной кэш кабинета не трогаем.
    api.invalidate("/api/v1/assistant/threads");
    try {
      const list = await refreshThreads();
      if (activeId && list.some((t) => t.id === activeId)) await selectThread(activeId);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  async function selectThread(id: string) {
    const seq = ++selectSeq.current;
    setActiveId(id);
    // detail НЕ обнуляем: иначе вместе с панелью размонтируется поле «Вопрос»
    // и набранный в этот момент текст теряется (гонка при создании диалога).
    setSwitching(true);
    setError("");
    setNotice("");
    try {
      const loaded = await api.assistantThread(id);
      if (selectSeq.current !== seq) return; // ответ устаревшего запроса
      setDetail(loaded);
    } catch (e: unknown) {
      if (selectSeq.current !== seq) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (selectSeq.current === seq) setSwitching(false);
    }
  }

  const createThread = async () => {
    setBusy(true);
    setError("");
    try {
      const thread = await api.assistantCreateThread();
      setThreads((t) => [thread, ...t]);
      await selectThread(thread.id);
      setListOpen(false);
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
      // Обновлённый тред уже пришёл из API — список патчим, без GET.
      setThreads((cur) => cur.map((t) => (t.id === updated.id ? { ...t, ...updated } : t)));
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
      const rest = threads.filter((t) => t.id !== detail.id);
      setThreads(rest);
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
      setThreads((cur) => cur.map((t) => (t.id === updated.id ? { ...t, ...updated } : t)));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const send = async () => {
    // busy: Ctrl+Enter не должен слать второй запрос, пока идёт ответ на первый.
    if (!detail || !input.trim() || switching || busy) return;
    const targetId = detail.id;
    const seq = ++selectSeq.current;
    const text = input.trim();
    // done уже сохранил ответ на сервере, но detail ещё старый: потоковые
    // пузыри держим до применения fresh, иначе ответ мигает в пустоту.
    const outcome = { done: false };
    setBusy(true);
    setStreaming(true);
    setError("");
    setNotice("");
    setInput("");
    setPendingUser(text);
    setStreamThreadId(targetId);
    setStreamText("");
    try {
      await api.assistantStreamMessage(
        targetId,
        text,
        {
          onToken: (t) => setStreamText((cur) => cur + t),
          onDone: (answer) => {
            outcome.done = true;
            // Сразу кладём сохранённый ход в detail: refetch лишь заменит его
            // серверной копией. Если refetch упадёт, ответ уже на экране и не
            // дублируется при повторной загрузке треда.
            const ts = new Date().toISOString();
            const saved: AssistantThreadMessage[] = [
              {
                id: answer.message_ids[0] ?? `${targetId}-local-${seq}-q`,
                role: "CLINICIAN",
                content: text,
                citations: [],
                topic: null,
                created_at: ts,
              },
              {
                id: answer.message_ids[1] ?? `${targetId}-local-${seq}-a`,
                role: "ASSISTANT",
                content: answer.answer,
                citations: answer.citations,
                topic: answer.topic,
                created_at: ts,
              },
            ];
            setDetail((cur) => {
              if (!cur || cur.id !== targetId) return cur;
              // Повторная загрузка могла уже принести эти сообщения: не дублируем.
              const have = new Set(cur.messages.map((m) => m.id));
              const added = saved.filter((m) => !have.has(m.id));
              if (added.length === 0) return cur;
              return {
                ...cur,
                messages: [...cur.messages, ...added],
                message_count: cur.message_count + added.length,
                updated_at: ts,
              };
            });
            // Список диалогов — из того же ответа: новый счётчик и время.
            setThreads((cur) =>
              cur.map((t) =>
                t.id === targetId
                  ? {
                      ...t,
                      message_count: t.message_count + answer.message_ids.length,
                      updated_at: ts,
                    }
                  : t,
              ),
            );
            setStreaming(false);
            setPendingUser("");
            setStreamThreadId(null);
            setStreamText("");
            if (answer.skipped_patient_ids.length > 0) {
              setNotice(
                `Пропущены пациенты без доступа: ${answer.skipped_patient_ids
                  .map((id) => patientLabels[id] ?? id)
                  .join(", ")}`,
              );
            }
          },
        },
      );
    } catch (e: unknown) {
      if (!outcome.done) setInput(text); // не терять вопрос при обрыве
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setStreaming(false);
      setPendingUser("");
      setStreamThreadId(null);
      setStreamText("");
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
    <div className="card chat-view">
      <h2>Ассистент врача</h2>
      <p className="muted small">
        Диалоги с картами пациентов: в ответ попадают карты, закреплённые за тредом;
        история диалога сохраняется и видна, но поиск идёт по текущему вопросу.
        {points !== null && <> Корпус: {points} чанков.</>}
      </p>

      <div className="chat-layout">
        <aside className={listOpen ? "chat-list open" : "chat-list"}>
          <button
            className="chat-list-toggle"
            aria-expanded={listOpen}
            onClick={() => setListOpen((v) => !v)}
          >
            Диалоги: {detail?.title ?? "—"}
          </button>
          <div className="chat-list-items">
            <div className="chat-list-actions">
              <button
                className="icon-btn"
                aria-label="Новый диалог"
                title="Новый диалог"
                onClick={() => void createThread()}
                disabled={busy}
              >
                +
              </button>
              <button
                className="icon-btn ghost"
                aria-label="Обновить диалоги"
                title="Обновить"
                onClick={() => void reloadAll()}
                disabled={busy}
              >
                ↻
              </button>
            </div>
            {threads.map((t) => (
              <button
                key={t.id}
                className={`chat-item${t.id === activeId ? " active" : ""}`}
                onClick={() => {
                  void selectThread(t.id);
                  setListOpen(false);
                }}
              >
                <span className="chat-item-title">{t.title}</span>
                <span className="meta">
                  {t.message_count} сообщ. · {t.patient_ids.length} пациент(ов)
                </span>
              </button>
            ))}
            {threads.length === 0 && <p className="muted small">Диалогов пока нет.</p>}
          </div>
        </aside>

        <section className="chat-pane">
          {detail === null ? (
            <p className="muted small">Создайте диалог, чтобы задать вопрос по базе знаний.</p>
          ) : (
            <>
              {renaming !== "" ? (
                <div className="chat-toolbar">
                  <input
                    value={renaming}
                    onChange={(e) => setRenaming(e.target.value)}
                    maxLength={200}
                    aria-label="Название диалога"
                  />
                  <button
                    className="icon-btn"
                    aria-label="Сохранить название"
                    title="Сохранить"
                    onClick={() => void renameThread()}
                    disabled={busy}
                  >
                    ✓
                  </button>
                  <button
                    className="icon-btn ghost"
                    aria-label="Отменить переименование"
                    title="Отмена"
                    onClick={() => setRenaming("")}
                  >
                    ✕
                  </button>
                </div>
              ) : (
                <div className="chat-toolbar">
                  <strong className="chat-title">{detail.title}</strong>
                  <button
                    className="icon-btn ghost"
                    aria-label="Переименовать"
                    title="Переименовать"
                    onClick={() => setRenaming(detail.title)}
                    disabled={busy}
                  >
                    ✎
                  </button>
                  <button
                    className="icon-btn danger"
                    aria-label="Удалить"
                    title="Удалить"
                    onClick={() => void deleteThread()}
                    disabled={busy}
                  >
                    ✕
                  </button>
                </div>
              )}

              <div className="chat-context">
                <div className="muted small">Пациенты в контексте (roster):</div>
                <div className="chips">
                  {patients.map((p) => {
                    const active = detail.patient_ids.includes(p.patient_id);
                    return (
                      <button
                        key={p.patient_id}
                        className={`chip${active ? " active" : ""}`}
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
                    <span className="muted small">Нет закреплённых пациентов.</span>
                  )}
                </div>
              </div>

              <div className="chat-messages">
                {detail.messages.length === 0 && (
                  <p className="muted small">Сообщений пока нет.</p>
                )}
                {detail.messages.map((m) => (
                  <div key={m.id} className={`bubble${m.role === "CLINICIAN" ? " mine" : ""}`}>
                    <span className="who">
                      {m.role === "CLINICIAN" ? "Врач" : "Ассистент"}
                      {m.topic ? ` · ${m.topic}` : ""}
                    </span>
                    {m.role === "ASSISTANT" ? (
                      <pre className="assistant-answer">{m.content}</pre>
                    ) : (
                      <div className="bubble-text">{m.content}</div>
                    )}
                    {m.citations.length > 0 && (
                      <div className="assistant-sources">
                        <strong>Источники:</strong>
                        <ul>
                          {m.citations.map((c, i) => (
                            // Номер совпадает с [n] в тексте ответа (порядок citations).
                            <li key={i}>
                              {i + 1}. «{c.source_title}» · {c.topic}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                ))}
                {pendingUser && streamThreadId === detail.id && (
                  <div className="bubble mine">
                    <span className="who">Врач</span>
                    <div className="bubble-text">{pendingUser}</div>
                  </div>
                )}
                {streaming && streamThreadId === detail.id && (
                  <div className="bubble">
                    <span className="who">Ассистент</span>
                    {streamText ? (
                      <pre className="assistant-answer">{streamText}</pre>
                    ) : (
                      <div className="bubble-text muted">Думаю…</div>
                    )}
                  </div>
                )}
              </div>

              <div className="chat-compose">
                <textarea
                  aria-label="Вопрос"
                  rows={2}
                  value={input}
                  maxLength={2000}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder="Например: какова динамика пациента из контекста и что говорит шкала PHQ-9?"
                  disabled={switching}
                  onKeyDown={(e) => {
                    // FR-47: Ctrl/Cmd+Enter — отправить, не уводя руки с клавиатуры.
                    if (!isSendShortcut(e)) return;
                    e.preventDefault();
                    void send();
                  }}
                />
                <button
                  className="icon-btn"
                  aria-label="Спросить"
                  title="Спросить"
                  disabled={busy || switching || !input.trim()}
                  onClick={() => void send()}
                >
                  {busy ? "…" : "→"}
                </button>
              </div>
              <p className="compose-hint">Ctrl+Enter — отправить</p>
            </>
          )}

          {notice && <p className="notice">{notice}</p>}
          {error && <p className="error">{error}</p>}
        </section>
      </div>

      <details className="kb-sources">
        <summary>Источники ({sources.length})</summary>
        <p className="muted small">
          Одобренные источники базы знаний. Скрытый источник остаётся в общей базе, но не
          попадает в вашу выдачу.
        </p>
        {!sourcesLoaded && <p className="muted small">Загрузка…</p>}
        {sourcesError && (
          <p className="error">Не удалось загрузить источники: {sourcesError}</p>
        )}
        {sourcesLoaded && sources.length === 0 && !sourcesError && (
          <p className="muted small">Одобренных источников пока нет.</p>
        )}
        {sources.map((s) => {
          const hidden = excludedIds.includes(s.guideline_id);
          const link = s.fulltext_url ?? s.url;
          const design = s.design?.design ?? "";
          return (
            <div key={s.id} className="kb-source">
              <div className="kb-source-title">{s.title}</div>
              <div className="row kb-source-meta">
                <span>{s.kind === "guideline" ? "Гайдлайн ВОЗ" : "Статья"}</span>
                {s.year ? <span>{s.year}</span> : null}
                <span>{KB_DESIGN_LABELS[design] ?? (design || "дизайн не определён")}</span>
                {link && (
                  <a href={link} target="_blank" rel="noreferrer">
                    Открыть источник
                  </a>
                )}
              </div>
              <label className="kb-source-toggle">
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
