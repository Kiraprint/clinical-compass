import { useCallback, useEffect, useState } from "react";
import {
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

type Tab = "screening" | "checkin" | "diary" | "chat" | "trend";

const TABS: { id: Tab; label: string }[] = [
  { id: "screening", label: "Скрининг" },
  { id: "checkin", label: "Ежедневный чек-ин" },
  { id: "diary", label: "Дневник" },
  { id: "chat", label: "Чат поддержки" },
  { id: "trend", label: "Мой тренд" },
];

export default function PatientView() {
  const [consent, setConsent] = useState<Consent | null>(null);
  const [consentChecked, setConsentChecked] = useState(false);
  const [tab, setTab] = useState<Tab>("screening");

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
      {tab === "screening" && <ScreeningTab />}
      {tab === "checkin" && <CheckinTab />}
      {tab === "diary" && <DiaryTab />}
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

function CheckinTab() {
  const [today, setToday] = useState<CheckinToday | null>(null);
  const [answers, setAnswers] = useState<Record<string, number>>({});
  const [done, setDone] = useState(false);

  const load = async () => {
    const t = await api.getTodayCheckin();
    setToday(t);
    setDone(t.done);
  };

  useEffect(() => {
    load();
  }, []);

  const submit = async () => {
    await api.completeCheckin(answers);
    setDone(true);
    await load();
  };

  if (done) {
    return (
      <div className="card">
        <h2>Чек-ин за сегодня выполнен</h2>
        <p style={{ color: "#5a6478" }}>Возвращайтесь завтра — вопросы адаптируются под ваше состояние.</p>
      </div>
    );
  }
  if (!today || today.items.length === 0) return <div className="card">Загрузка…</div>;

  return (
    <div className="card">
      <h2>Как вы себя чувствуете сегодня?</h2>
      {today.items.map((item) => (
        <div key={item.key} className="qa">
          <p>{item.text}</p>
          <div className="row">
            {[0, 1, 2, 3].map((v) => (
              <button key={v} className={answers[item.key] === v ? "on" : ""} onClick={() => setAnswers((a) => ({ ...a, [item.key]: v }))}>
                {v}
              </button>
            ))}
          </div>
        </div>
      ))}
      <button onClick={submit}>Отправить</button>
    </div>
  );
}

function DiaryTab() {
  const [content, setContent] = useState("");
  const [mood, setMood] = useState(5);
  const [sleep, setSleep] = useState(7);
  const [items, setItems] = useState<DiaryEntry[]>([]);

  const load = async () => setItems((await api.getDiary()).items);
  useEffect(() => {
    load();
  }, []);

  const add = async () => {
    if (!content.trim()) return;
    await api.postDiary({ content, mood, sleep_h: sleep, tags: [] });
    setContent("");
    await load();
  };

  return (
    <div className="card">
      <h2>Дневник самочувствия</h2>
      <label>Как прошёл день?</label>
      <textarea rows={3} value={content} onChange={(e) => setContent(e.target.value)} />
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
      <button onClick={add}>Сохранить запись</button>
      <div style={{ marginTop: 14 }}>
        {items.map((entry) => (
          <div key={entry.id} className="msg">
            <b>{entry.mood ? `Настроение: ${entry.mood}/10` : ""}</b> {entry.content}
          </div>
        ))}
      </div>
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
            <b>{m.sender === "SYSTEM" ? "Помощник: " : "Вы: "}</b>
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