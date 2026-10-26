import { useEffect, useState } from "react";
import { api, ApiError, ChatInsight, PatientChat, PatientChatMessage } from "../api";
import AssistantTab from "./AssistantTab";

const SENDER_LABELS: Record<string, string> = {
  CLINICIAN: "Врач",
  PATIENT: "Пациент",
  SYSTEM: "Помощник",
};

const KIND_LABELS: Record<ChatInsight["kind"], string> = {
  symptom: "Симптом",
  medication: "Препарат",
  adherence: "Приверженность",
  sleep: "Сон",
  substance: "ПАВ",
  risk: "Риск",
  other: "Другое",
};

function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// FR-29: отдельный экран врача «Чаты» — два раздела в сегмент-контроле.
export default function ChatsView({
  mode: initialMode,
  patientId,
  onSelectPatient,
  onModeChange,
}: {
  mode?: "assistant" | "patients";
  patientId?: string | null;
  onSelectPatient: (id: string | null) => void;
  onModeChange?: (mode: "assistant" | "patients") => void;
}) {
  // FR-32: режим задаёт левое меню («Ассистент» / «Чаты с пациентами»),
  // сегмент-контроль внутри экрана остаётся быстрым переключателем.
  const [mode, setMode] = useState<"assistant" | "patients">(
    initialMode ?? (patientId ? "patients" : "assistant"),
  );

  useEffect(() => {
    if (initialMode) setMode(initialMode);
  }, [initialMode]);

  // Переход из карточки пациента («Открыть в чатах») сразу показывает переписку.
  useEffect(() => {
    if (patientId) setMode("patients");
  }, [patientId]);

  const switchMode = (next: "assistant" | "patients") => {
    setMode(next);
    onModeChange?.(next);
  };

  return (
    <div className="chat-view">
      <div className="segmented" role="tablist" aria-label="Раздел чатов">
        <button
          role="tab"
          aria-selected={mode === "assistant"}
          className={mode === "assistant" ? "active" : ""}
          onClick={() => switchMode("assistant")}
        >
          Ассистент
        </button>
        <button
          role="tab"
          aria-selected={mode === "patients"}
          className={mode === "patients" ? "active" : ""}
          onClick={() => switchMode("patients")}
        >
          Пациенты
        </button>
      </div>
      {mode === "assistant" ? (
        <AssistantTab />
      ) : (
        <PatientChats selected={patientId ?? null} onSelect={onSelectPatient} />
      )}
    </div>
  );
}

function PatientChats({
  selected,
  onSelect,
}: {
  selected: string | null;
  onSelect: (id: string | null) => void;
}) {
  const [chats, setChats] = useState<PatientChat[]>([]);
  const [messages, setMessages] = useState<PatientChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  // На узком экране список пациентов сворачивается в кнопку над диалогом.
  const [listOpen, setListOpen] = useState(false);
  // FR-31: черновики фактов из переписки и состояние фичи (локальная модель).
  const [insights, setInsights] = useState<ChatInsight[]>([]);
  const [insightsOff, setInsightsOff] = useState(false);
  const [insightsBusy, setInsightsBusy] = useState(false);
  const [insightsNote, setInsightsNote] = useState("");

  const loadChats = async (): Promise<PatientChat[]> => {
    try {
      const r = await api.clinicianChats();
      setChats(r.items);
      setError("");
      return r.items;
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      return [];
    }
  };

  useEffect(() => {
    void (async () => {
      const items = await loadChats();
      setLoaded(true);
      if (!selected && items.length > 0) onSelect(items[0].patient_id);
    })();
    // Первичная загрузка: дальше список обновляется после отправки/кнопкой «Обновить».
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadMessages = async (patientId: string) => {
    try {
      const r = await api.clinicianChatList(patientId);
      setMessages(r.items);
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      setMessages([]);
    }
  };

  const analyzeChat = async () => {
    if (!selected) return;
    setInsightsBusy(true);
    setInsightsNote("");
    try {
      const r = await api.analyzeChat(selected);
      setInsightsNote(
        `Добавлено: ${r.created}${r.skipped ? `, уже было: ${r.skipped}` : ""}`,
      );
      const list = await api.chatInsights(selected, "pending");
      setInsights(list.items);
      setInsightsOff(false);
    } catch (e: unknown) {
      if (e instanceof ApiError && e.code === "CHAT_INSIGHTS_DISABLED") {
        setInsightsOff(true);
        setInsights([]);
      } else if (e instanceof ApiError && e.code === "CHAT_INSIGHTS_EMPTY") {
        setInsightsNote("В переписке пока нет сообщений — анализировать нечего.");
      } else {
        setInsightsNote(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setInsightsBusy(false);
    }
  };

  const decideInsight = async (id: string, status: "accepted" | "rejected") => {
    if (!selected) return;
    try {
      await api.decideChatInsight(selected, id, status);
      setInsights((cur) => cur.filter((i) => i.id !== id));
    } catch (e: unknown) {
      setInsightsNote(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    if (!selected) {
      setMessages([]);
      return;
    }
    let cancelled = false;
    api
      .clinicianChatList(selected)
      .then((r) => {
        if (!cancelled) setMessages(r.items);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setMessages([]);
        setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  // FR-31: черновики фактов выбранного пациента; выключенная фича — не ошибка.
  useEffect(() => {
    setInsightsNote("");
    setInsightsBusy(false);
    if (!selected) {
      setInsights([]);
      setInsightsOff(false);
      return;
    }
    let cancelled = false;
    api
      .chatInsights(selected, "pending")
      .then((r) => {
        if (cancelled) return;
        setInsights(r.items);
        setInsightsOff(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setInsights([]);
        setInsightsOff(e instanceof ApiError && e.code === "CHAT_INSIGHTS_DISABLED");
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  const active = chats.find((c) => c.patient_id === selected) ?? null;
  const activeName = active?.fio ?? "Пациент";

  const send = async () => {
    // busy: Enter/Ctrl+Enter не должны дублировать сообщение, пока первое в пути.
    if (!selected || !input.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const msg = await api.clinicianChatPost(selected, input.trim());
      setMessages((cur) => [...cur, msg]);
      setInput("");
      await loadChats();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card chat-view">
      <h2>Переписка с пациентами</h2>
      <p className="muted small">
        Только пациенты, закреплённые за вами. Превью последнего сообщения — в списке,
        полная история — в диалоге.
      </p>

      <div className="chat-layout">
        <aside className={listOpen ? "chat-list open" : "chat-list"}>
          <button
            className="chat-list-toggle"
            aria-expanded={listOpen}
            onClick={() => setListOpen((v) => !v)}
          >
            Диалоги: {activeName}
          </button>
          <div className="chat-list-items">
            <div className="chat-list-actions">
              <button
                className="icon-btn ghost"
                aria-label="Обновить переписки"
                title="Обновить"
                onClick={() => {
                  void loadChats();
                  if (selected) void loadMessages(selected);
                }}
              >
                ↻
              </button>
            </div>
            {chats.map((c) => (
              <button
                key={c.patient_id}
                className={`chat-item${c.patient_id === selected ? " active" : ""}`}
                aria-current={c.patient_id === selected ? "true" : undefined}
                onClick={() => {
                  onSelect(c.patient_id);
                  setListOpen(false);
                }}
              >
                <span className="chat-item-title">{c.fio ?? "Пациент"}</span>
                <span className="meta">
                  {SENDER_LABELS[c.last_sender ?? ""] ?? "—"} · {fmtTime(c.last_at)} ·{" "}
                  {c.message_count} сообщ.
                </span>
                {c.preview && <span className="preview">{c.preview}</span>}
              </button>
            ))}
            {loaded && chats.length === 0 && (
              <p className="muted small">Переписок пока нет.</p>
            )}
          </div>
        </aside>

        <section className="chat-pane">
          {!selected ? (
            <p className="muted small">Выберите пациента с перепиской.</p>
          ) : (
            <>
              <div className="chat-toolbar">
                <strong className="chat-title">{activeName}</strong>
                <button
                  className="icon-btn ghost"
                  aria-label="Обновить диалог"
                  title="Обновить"
                  onClick={() => void loadMessages(selected)}
                >
                  ↻
                </button>
                {!insightsOff && (
                  <button
                    className="icon-btn ghost"
                    aria-label="Анализ переписки"
                    title="Анализ переписки"
                    disabled={insightsBusy}
                    onClick={() => void analyzeChat()}
                  >
                    ✨
                  </button>
                )}
              </div>

              {insightsOff ? (
                <p className="muted small">
                  Анализ переписки выключен (нужна локальная модель, docs/17).
                </p>
              ) : (
                <div className="insight-panel">
                  <div className="insight-head">
                    <strong>
                      Факты из переписки{insights.length > 0 ? ` (${insights.length})` : ""}
                    </strong>
                  </div>
                  {insightsNote && <p className="muted small">{insightsNote}</p>}
                  {insights.length === 0 ? (
                    <p className="muted small">
                      Новых черновиков нет — запустите анализ кнопкой «Анализ переписки».
                    </p>
                  ) : (
                    insights.map((i) => (
                      <div className="insight-item" key={i.id}>
                        <span className="insight-kind">{KIND_LABELS[i.kind]}</span>
                        <span className="insight-text">{i.text}</span>
                        <span className="insight-actions">
                          <button className="ghost" onClick={() => void decideInsight(i.id, "accepted")}>
                            Принять
                          </button>
                          <button className="ghost" onClick={() => void decideInsight(i.id, "rejected")}>
                            Отклонить
                          </button>
                        </span>
                      </div>
                    ))
                  )}
                </div>
              )}

              <div className="chat-messages">
                {messages.length === 0 && (
                  <p className="muted small">Сообщений пока нет.</p>
                )}
                {messages.map((m) => (
                  <div
                    key={m.id}
                    className={`bubble${m.sender === "CLINICIAN" ? " mine" : ""}`}
                  >
                    <span className="who">
                      {SENDER_LABELS[m.sender] ?? m.sender} · {fmtTime(m.created_at)}
                    </span>
                    <div className="bubble-text">{m.content}</div>
                  </div>
                ))}
              </div>

              <div className="chat-compose">
                <input
                  aria-label="Сообщение пациенту"
                  placeholder="Сообщение пациенту"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    // FR-47: Enter (в т.ч. Ctrl/Cmd+Enter — та же клавиша) отправляет.
                    if (e.key === "Enter") void send();
                  }}
                />
                <button
                  className="icon-btn"
                  aria-label="Отправить"
                  title="Отправить"
                  disabled={busy || !input.trim()}
                  onClick={() => void send()}
                >
                  →
                </button>
              </div>
              <p className="compose-hint">Ctrl+Enter — отправить</p>
            </>
          )}
          {error && <p className="error">{error}</p>}
        </section>
      </div>
    </div>
  );
}
