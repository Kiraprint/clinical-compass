import { useCallback, useEffect, useState } from "react";
import {
  AdherenceRecord,
  api,
  ApiError,
  Assessment,
  CheckinToday,
  ChatMessage,
  Consent,
  DiaryEntry,
  Questionnaire,
  Timeline,
} from "../api";
import TrendChart from "../TrendChart";

type Tab = "today" | "screening" | "diary" | "chat" | "trend" | "adherence";

// «Сегодня» — один ежедневный экран: чек-ин + дневник + приём лекарств.
// Дневник и Приверженность остаются как история, запись идёт только из «Сегодня».
const TABS: { id: Tab; label: string }[] = [
  { id: "today", label: "Сегодня" },
  { id: "screening", label: "Скрининг" },
  { id: "diary", label: "Дневник" },
  { id: "adherence", label: "Приверженность" },
  { id: "chat", label: "Чат поддержки" },
  { id: "trend", label: "Мой тренд" },
];

export default function PatientView() {
  const [consent, setConsent] = useState<Consent | null>(null);
  const [consentChecked, setConsentChecked] = useState(false);
  const [tab, setTab] = useState<Tab>("today");

  const loadConsent = useCallback(async () => {
    try {
      setConsent(await api.getConsent());
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setConsent(null);
    } finally {
      setConsentChecked(true);
    }
  }, []);

  useEffect(() => {
    loadConsent();
  }, [loadConsent]);

  const signConsent = async () => {
    await api.postConsent({ version: 1 });
    await loadConsent();
  };

  const revokeConsent = async () => {
    await api.revokeConsent();
    await loadConsent();
  };

  if (!consentChecked) return <div className="card">Загрузка…</div>;

  if (!consent) {
    return (
      <div className="card">
        <h2>Согласие на обработку персональных данных (152-ФЗ)</h2>
        <p style={{ color: "#5a6478", fontSize: 14 }}>
          Для работы с инструментом требуется ваше согласие на обработку персональных данных, включая
          специальные категории (данные о состоянии здоровья). Без согласия сервис работает только в
          демо-режиме без сохранения персональных данных.
        </p>
        <button onClick={signConsent}>Даю согласие</button>
      </div>
    );
  }

  return (
    <>
      <div className="alert-info row">
        <span style={{ fontSize: 13 }}>Согласие активно (v{consent.version}, от {consent.signed_at.slice(0, 10)})</span>
        <button className="ghost" onClick={revokeConsent}>
          Отозвать
        </button>
      </div>
      <div className="tabs">
        {TABS.map((t) => (
          <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>
      {tab === "today" && <TodayTab />}
      {tab === "screening" && <ScreeningTab />}
      {tab === "diary" && <DiaryTab />}
      {tab === "adherence" && <AdherenceTab />}
      {tab === "chat" && <ChatTab />}
      {tab === "trend" && <TrendTab />}
    </>
  );
}

function ScreeningTab() {
  const [catalog, setCatalog] = useState<Questionnaire[]>([]);
  const [assigned, setAssigned] = useState<{ code: string; title: string }[]>([]);
  const [chosenCode, setChosenCode] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [questions, setQuestions] = useState<{ key: string; text: string; max: number }[]>([]);
  const [answers, setAnswers] = useState<Record<string, number>>({});
  const [result, setResult] = useState<Assessment | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([api.getQuestionnaires(), api.prescribedScales()])
      .then(([q, p]) => {
        setCatalog(q);
        setAssigned(p.scales);
        // Одна назначенная — выбираем её автоматически; несколько — пациент выберет.
        setChosenCode(p.scales.length === 1 ? p.scales[0].code : null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  const start = async () => {
    setError("");
    try {
      const run = await api.startScreening(chosenCode ?? undefined);
      const scale = catalog.find((s) => s.code === run.questionnaire_code);
      setRunId(run.run_id);
      setQuestions((scale?.items ?? []).map((i) => ({ key: i.key, text: i.text, max: i.max_value })));
      setAnswers({});
      setResult(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // Открытый брошенный скрининг: закрываем его и пробуем ещё раз.
        await api.abandonScreening();
        return start();
      }
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const pick = (key: string, value: number) => setAnswers((a) => ({ ...a, [key]: value }));

  const finish = async () => {
    if (!runId) return;
    try {
      await api.submitAnswers(runId, answers);
      const assessment = await api.finishRun(runId);
      setResult(assessment);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  if (result) {
    return (
      <div className="card">
        <h2>Результат скрининга — {result.questionnaire_code}</h2>
        <div className="row">
          <span className={`badge ${result.triage}`}>{result.triage}</span>
          <span>
            Балл {result.total_score} · {result.severity}
          </span>
        </div>
        {result.escalation && (
          <div className="alert-danger" style={{ marginTop: 12 }}>
            <b>Требуется внимание.</b> Наши специалисты уведомлены. Если вам сейчас тяжело — позвоните:
            {result.escalation.help_resources.map((r) => (
              <div key={r.phone}>
                {r.title}: <b>{r.phone}</b>
              </div>
            ))}
          </div>
        )}
        <button style={{ marginTop: 12 }} onClick={() => setResult(null)}>
          Новый скрининг
        </button>
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Скрининг тревоги и депрессии</h2>
      {!runId ? (
        <>
          <p style={{ color: "#5a6478", fontSize: 14 }}>
            {assigned.length > 1
              ? "Шкалы назначены вашим врачом — выберите одну:"
              : "Шкала назначена вашим врачом:"}
          </p>
          {assigned.length > 1 && (
            <div className="row" style={{ gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
              {assigned.map((s) => (
                <button
                  key={s.code}
                  className={chosenCode === s.code ? "on" : ""}
                  onClick={() => setChosenCode(s.code)}
                >
                  {s.title}
                </button>
              ))}
            </div>
          )}
          {assigned.length === 1 && (
            <b style={{ display: "block", marginBottom: 12 }}>{assigned[0]?.title || "…"}</b>
          )}
          <div style={{ marginTop: 4 }}>
            <button onClick={start} disabled={!chosenCode}>
              Начать скрининг
            </button>
          </div>
        </>
      ) : (
        <>
          {questions.map((q) => (
            <div key={q.key} className="qa">
              <p>{q.text}</p>
              <div className="row">
                {[0, 1, 2, 3].map((v) => (
                  <button
                    key={v}
                    className={answers[q.key] === v ? "on" : ""}
                    onClick={() => pick(q.key, v)}
                  >
                    {v}
                  </button>
                ))}
              </div>
            </div>
          ))}
          {error && <p className="error">{error}</p>}
          <button onClick={finish}>Завершить</button>
        </>
      )}
    </div>
  );
}

const ADH_LABELS: Record<string, string> = { taken: "Принял", partial: "Частично", missed: "Пропустил" };

const sameDay = (iso: string, ref: Date) => new Date(iso).toDateString() === ref.toDateString();

function TodayTab() {
  const [today, setToday] = useState<CheckinToday | null>(null);
  const [answers, setAnswers] = useState<Record<string, number>>({});
  const [done, setDone] = useState(false);
  const [content, setContent] = useState("");
  const [mood, setMood] = useState(5);
  const [sleep, setSleep] = useState(7);
  const [entriesToday, setEntriesToday] = useState<DiaryEntry[]>([]);
  const [status, setStatus] = useState<"taken" | "partial" | "missed" | null>(null);
  const [medication, setMedication] = useState("");
  const [adhNote, setAdhNote] = useState("");
  const [adhToday, setAdhToday] = useState<AdherenceRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState("");
  const [err, setErr] = useState("");

  const load = async () => {
    const [t, d, a] = await Promise.all([api.getTodayCheckin(), api.getDiary(), api.getMyAdherence()]);
    setToday(t);
    setDone(t.done);
    const ref = new Date();
    setEntriesToday(d.items.filter((e) => sameDay(e.created_at, ref)));
    setAdhToday(a.today);
  };

  useEffect(() => {
    load().catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  }, []);

  const save = async () => {
    setErr("");
    setSaved("");
    const items = today?.items ?? [];
    const missing = done ? [] : items.filter((i) => answers[i.key] === undefined);
    setBusy(true);
    const parts: string[] = [];
    try {
      if (!done && items.length > 0 && missing.length === 0) {
        await api.completeCheckin(answers);
        parts.push("чек-ин");
      }
      if (content.trim()) {
        await api.postDiary({ content, mood, sleep_h: sleep, tags: [] });
        setContent("");
        parts.push("дневник");
      }
      if (!adhToday && status) {
        await api.recordAdherence({
          medication: medication.trim() || undefined,
          status,
          note: adhNote.trim() || undefined,
        });
        setMedication("");
        setAdhNote("");
        parts.push("приём лекарств");
      }
      await load();
      setSaved(parts.length > 0 ? `Сохранено: ${parts.join(" · ")}` : "Всё уже отмечено за сегодня");
      // Неполный чек-ин не блокирует дневник и отметку о приёме — говорим, чего не хватает.
      if (missing.length > 0 && missing.length < items.length) {
        setErr(`Чек-ин не сохранён: ответьте ещё на ${missing.length}`);
      }
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!today) return <div className="card">Загрузка…</div>;

  return (
    <div className="card">
      <h2>Сегодня</h2>
      <p style={{ color: "#5a6478", fontSize: 14 }}>
        {new Date().toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" })} — чек-ин,
        дневник и приём лекарств в одной форме.
      </p>

      <h3>Чек-ин</h3>
      {done ? (
        <p style={{ color: "#2e7d32", fontWeight: 600 }}>Чек-ин за сегодня выполнен</p>
      ) : today.items.length === 0 ? (
        <p style={{ color: "#5a6478" }}>Вопросы появятся после первого скрининга.</p>
      ) : (
        today.items.map((item) => (
          <div key={item.key} className="qa">
            <p>{item.text}</p>
            <div className="row">
              {Array.from({ length: item.max_value + 1 }, (_, v) => v).map((v) => (
                <button
                  key={v}
                  className={answers[item.key] === v ? "on" : ""}
                  onClick={() => setAnswers((a) => ({ ...a, [item.key]: v }))}
                >
                  {v}
                </button>
              ))}
            </div>
          </div>
        ))
      )}

      <h3>Дневник</h3>
      <label htmlFor="today-diary">Как прошёл день?</label>
      <textarea id="today-diary" rows={3} value={content} onChange={(e) => setContent(e.target.value)} />
      <div className="two">
        <div>
          <label>Настроение (1–10): {mood}</label>
          <input type="range" min={1} max={10} value={mood} onChange={(e) => setMood(Number(e.target.value))} />
        </div>
        <div>
          <label>Сон, часов: {sleep}</label>
          <input type="range" min={0} max={12} step={0.5} value={sleep} onChange={(e) => setSleep(Number(e.target.value))} />
        </div>
      </div>
      {entriesToday.length > 0 && (
        <div style={{ marginTop: 8 }}>
          {entriesToday.map((e) => (
            <div key={e.id} className="msg">
              <b>{e.mood ? `Настроение: ${e.mood}/10` : ""}</b> {e.content}
            </div>
          ))}
        </div>
      )}

      <h3>Приём лекарств</h3>
      {adhToday ? (
        <p style={{ color: "#2e7d32", fontWeight: 600 }}>
          За сегодня отмечено: {ADH_LABELS[adhToday.status] ?? adhToday.status}
          {adhToday.medication ? ` (${adhToday.medication})` : ""}
        </p>
      ) : (
        <>
          <div style={{ display: "flex", gap: 8, margin: "8px 0" }}>
            {(["taken", "partial", "missed"] as const).map((s) => (
              <button key={s} className={status === s ? "active" : "ghost"} onClick={() => setStatus(s)}>
                {ADH_LABELS[s]}
              </button>
            ))}
          </div>
          <input
            placeholder="Препарат (напр. Авиандр) — необязательно"
            value={medication}
            onChange={(e) => setMedication(e.target.value)}
          />
          <input placeholder="Заметка (необязательно)" value={adhNote} onChange={(e) => setAdhNote(e.target.value)} />
        </>
      )}

      <div style={{ marginTop: 14, display: "flex", gap: 10, alignItems: "center" }}>
        <button onClick={save} disabled={busy}>
          {busy ? "Сохраняем…" : "Сохранить за сегодня"}
        </button>
        {saved && <span style={{ color: "#2e7d32", fontSize: 13 }}>{saved}</span>}
      </div>
      {err && <p className="error">{err}</p>}
    </div>
  );
}

function DiaryTab() {
  const [items, setItems] = useState<DiaryEntry[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.getDiary().then((d) => setItems(d.items)).catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  }, []);

  return (
    <div className="card">
      <h2>Дневник самочувствия</h2>
      <p style={{ color: "#5a6478", fontSize: 14 }}>
        История записей. Новую запись добавляйте на вкладке «Сегодня» — вместе с чек-ином и отметкой о приёме лекарств.
      </p>
      {err && <p className="error">{err}</p>}
      {items.length === 0 && <p style={{ color: "#5a6478" }}>Пока пусто.</p>}
      {items.map((entry) => (
        <div key={entry.id} className="msg">
          <b>{entry.mood ? `Настроение: ${entry.mood}/10` : ""}</b> {entry.content}
          <div style={{ color: "#5a6478", fontSize: 12 }}>
            {new Date(entry.created_at).toLocaleString("ru-RU")}
            {entry.sleep_h != null ? ` · сон ${entry.sleep_h} ч` : ""}
          </div>
        </div>
      ))}
    </div>
  );
}

function AdherenceTab() {
  const [data, setData] = useState<{ items: AdherenceRecord[]; today: AdherenceRecord | null } | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    api
      .getMyAdherence(14)
      .then(setData)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  }, []);

  const items = data?.items ?? [];
  const total = items.length;
  const score = items.reduce((acc, r) => acc + (r.status === "taken" ? 1 : r.status === "partial" ? 0.5 : 0), 0);
  const pct = total > 0 ? Math.round((score / total) * 1000) / 10 : null;

  return (
    <div className="card">
      <h2>Приверженность лечению</h2>
      <p style={{ color: "#5a6478", fontSize: 14 }}>
        Отметки за последние 14 дней. Сегодняшнюю отметку ставьте на вкладке «Сегодня».
      </p>
      <p>
        <b>Приверженность: {pct != null ? `${pct}%` : "—"}</b>{" "}
        <span style={{ color: "#5a6478", fontSize: 13 }}>({total} отметок за 14 дней)</span>
      </p>
      {err && <p className="error">{err}</p>}
      {total === 0 && <p style={{ color: "#5a6478" }}>Отметок пока нет.</p>}
      {items.map((r) => (
        <div key={r.id} className="msg">
          <b>{ADH_LABELS[r.status] ?? r.status}</b>
          {r.medication ? ` · ${r.medication}` : ""}
          {r.note ? ` · ${r.note}` : ""}
          <div style={{ color: "#5a6478", fontSize: 12 }}>{new Date(r.created_at).toLocaleString("ru-RU")}</div>
        </div>
      ))}
    </div>
  );
}

function ChatTab() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [alert, setAlert] = useState<string | null>(null);

  const load = async () => {
    setMessages((await api.getChat()).items);
  };
  useEffect(() => {
    load();
  }, []);

  const send = async () => {
    if (!input.trim()) return;
    const reply = await api.postChat(input);
    setInput("");
    setAlert(reply.reply.help_resources.length ? "Рекомендуем связаться со специалистом" : null);
    await load();
  };

  return (
    <div className="card">
      <h2>Чат поддержки</h2>
      <div style={{ maxHeight: 320, overflowY: "auto", marginBottom: 10 }}>
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.sender === "SYSTEM" ? "system" : ""}`}>
            <b>{m.sender === "SYSTEM" ? "Помощник: " : m.sender === "CLINICIAN" ? "Врач: " : "Вы: "}</b>
            {m.content}
          </div>
        ))}
      </div>
      {alert && <div className="alert-danger">{alert}: единый телефон доверия 8-800-2000-122, экстренная служба 112</div>}
      <div className="row">
        <input value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send()} />
        <button onClick={send}>Отправить</button>
      </div>
    </div>
  );
}

function TrendTab() {
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  useEffect(() => {
    api.getTimeline(90).then(setTimeline);
  }, []);
  if (!timeline) return <div className="card">Загрузка…</div>;
  return (
    <div className="card">
      <h2>Динамика за 90 дней</h2>
      <TrendChart timeline={timeline} />
      {timeline.runs.length === 0 && <p style={{ color: "#5a6478" }}>Пройдите первый скрининг, чтобы увидеть тренд.</p>}
    </div>
  );
}