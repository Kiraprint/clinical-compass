import { useCallback, useEffect, useState } from "react";
import {
  AdherenceRecord,
  AdherenceSummary,
  api,
  ApiError,
  Assessment,
  CheckinState,
  ChatMessage,
  Consent,
  DiaryEntry,
  MoodScale,
  Questionnaire,
  Timeline,
} from "../api";
import TrendChart from "../TrendChart";

type Tab = "today" | "screening" | "diary" | "chat" | "trend" | "adherence" | "contacts";

// «Сегодня» — один ежедневный экран: чек-ин + дневник + приём лекарств.
// Дневник и Приверженность остаются как история, запись идёт только из «Сегодня».
const TABS: { id: Tab; label: string }[] = [
  { id: "today", label: "Сегодня" },
  { id: "screening", label: "Скрининг" },
  { id: "diary", label: "Дневник" },
  { id: "adherence", label: "Приверженность" },
  { id: "chat", label: "Чат поддержки" },
  { id: "trend", label: "Мой тренд" },
  { id: "contacts", label: "Контакты" },
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
      {tab === "contacts" && <ContactsTab />}
    </>
  );
}

function ScreeningTab() {
  const [catalog, setCatalog] = useState<Questionnaire[]>([]);
  const [assigned, setAssigned] = useState<{ code: string; title: string }[]>([]);
  const [chosenCode, setChosenCode] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [questions, setQuestions] = useState<
    { key: string; text: string; max: number; anchors: string[] }[]
  >([]);
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
      setQuestions(
        (scale?.items ?? []).map((i) => ({
          key: i.key, text: i.text, max: i.max_value, anchors: i.anchors ?? [],
        })),
      );
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
                {Array.from({ length: q.max + 1 }, (_, v) => v).map((v) => (
                  <button
                    key={v}
                    className={answers[q.key] === v ? "on" : ""}
                    title={`${v} из ${q.max}`}
                    style={{ fontSize: 13, flex: "1 1 auto" }}
                    onClick={() => pick(q.key, v)}
                  >
                    {q.anchors[v] ?? String(v)}
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

// Настроение в дневнике — 5-точечная шкала с эмодзи (docs/16 §6), 0 — очень плохо.
const MOOD_OPTIONS = [
  { emoji: "😣", label: "Очень плохо" },
  { emoji: "😟", label: "Плохо" },
  { emoji: "😐", label: "Так себе" },
  { emoji: "🙂", label: "Хорошо" },
  { emoji: "😄", label: "Отлично" },
];

// mood_scale = "1-10" у историчных записей: показываем значение как есть, без эмодзи.
const moodText = (mood: number | null, scale: MoodScale) => {
  if (mood == null) return "";
  if (scale !== "0-4") return `настроение ${mood}/10`;
  const option = MOOD_OPTIONS[mood];
  return option ? `${option.emoji} ${option.label}` : `настроение ${mood}`;
};

// Спокойный текст без обвинения и без обещания мгновенного контакта (docs/16 §3).
// Последняя строка зависит только от того, что сервер подтвердил: дошёл ли сигнал врачу
// (clinician_notified). Без врача обещать «вас видит врач» нельзя.
const SAFETY_BLOCKS: Record<
  string,
  { title: string; paragraphs: string[]; last: { notified: string; notNotified: string } }
> = {
  signal: {
    title: "Ваша безопасность важнее всего",
    paragraphs: [
      "Спасибо, что ответили честно. Пожалуйста, постарайтесь оставаться в безопасности и, если есть возможность, будьте рядом с кем-то, кому вы доверяете.",
      "Если у вас есть намерение действовать или вам трудно оставаться в безопасности, обратитесь за экстренной помощью по местному номеру — 112 (единый номер экстренных служб) или в ближайшее отделение скорой помощи.",
    ],
    last: {
      notified:
        "Ваш ответ видит врач, но просмотр сообщений командой может быть не мгновенным: этот экран не линия экстренной помощи и не заменяет обращение к врачу.",
      notNotified:
        "Ответ сохранён. Закреплённого врача у вас нет — если помощь нужна сейчас, обратитесь за экстренной помощью (112) или к близким.",
    },
  },
  review: {
    title: "Спасибо за ваш ответ",
    paragraphs: [
      "Спасибо, что ответили. Пожалуйста, оставайтесь в безопасности и позаботьтесь о себе сегодня.",
      "Если станет тяжелее или появятся мысли о том, чтобы причинить себе вред, обратитесь за экстренной помощью по местному номеру — 112.",
    ],
    last: {
      notified:
        "Ответы видит ваш врач в карте. Просмотр сообщений командой может быть не мгновенным — это не обещание срочного контакта.",
      notNotified:
        "Ответ сохранён. Закреплённого врача у вас нет — если станет тяжелее, обратитесь за экстренной помощью (112) или к близким.",
    },
  },
  unassessed: {
    title: "Риск не оценён",
    paragraphs: [
      "Вы не ответили на вопрос о мыслях о самоубийстве, поэтому риск сегодня не оценён. Пропуск не означает «нет».",
      "Если есть мысли о том, чтобы причинить себе вред, обратитесь за экстренной помощью по местному номеру — 112.",
    ],
    last: {
      notified:
        "Ответ видит ваш врач в карте — обсудите с ним состояние. Просмотр может быть не мгновенным: это не линия экстренной помощи.",
      notNotified:
        "Ответ сохранён. Закреплённого врача у вас нет — пожалуйста, обсудите состояние с близкими или обратитесь к специалисту.",
    },
  },
};

function SafetyBlock({ safety, notified }: { safety: string; notified: boolean }) {
  const info = SAFETY_BLOCKS[safety];
  if (!info) return null;
  const last = notified ? info.last.notified : info.last.notNotified;
  return (
    <div className="safety" role="status">
      <h3>{info.title}</h3>
      {[...info.paragraphs, last].map((p) => (
        <p key={p}>{p}</p>
      ))}
    </div>
  );
}

// Адаптивный чек-ин: один шаг за раз, ответы уходят только за текущий шаг (docs/16 §4).
function CheckinCard() {
  const [state, setState] = useState<CheckinState | null>(null);
  const [stepAnswers, setStepAnswers] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const apply = (next: CheckinState) => {
    setState(next);
    setStepAnswers({});
  };

  useEffect(() => {
    api
      .todayCheckin()
      .then(apply)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  }, []);

  const run = async (action: () => Promise<CheckinState>) => {
    setErr("");
    setBusy(true);
    try {
      apply(await action());
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
      // Сеанс уже завершён (409 CHECKIN_ALREADY_DONE) — подтягиваем итоговое состояние.
      if (e instanceof ApiError && e.code === "CHECKIN_ALREADY_DONE") {
        api.todayCheckin().then(apply).catch(() => {});
      }
    } finally {
      setBusy(false);
    }
  };

  if (!state) return <p style={{ color: "#5a6478" }}>{err || "Загрузка чек-ина…"}</p>;

  const items = state.step.items;
  const answeredInStep = items.filter((i) => stepAnswers[i.id] !== undefined).length;
  const allAnswered = items.length > 0 && answeredInStep === items.length;
  // «Отправить» — на последнем шаге: безопасность или модули закрывают сеанс.
  const lastStep =
    (state.phase === "safety" || state.phase === "modules") &&
    state.progress.answered + items.length >= state.progress.planned;

  const submit = () => {
    const sessionId = state.session_id;
    if (!sessionId) return;
    run(() => api.submitCheckin(sessionId, stepAnswers));
  };

  return (
    <>
      {state.status === "completed" ? (
        <>
          <p style={{ color: "#2e7d32", fontWeight: 600 }}>Чек-ин за сегодня выполнен</p>
          <p style={{ color: "#5a6478", fontSize: 13 }}>
            {state.safety === "none"
              ? "Ответы сохранены. Если за вами закреплён врач, он увидит их в карте. Это наблюдения и их динамика, а не диагноз."
              : state.clinician_notified
                ? "Ваши ответы за сегодня видит врач в карте. Это отдельные наблюдения и их динамика, а не диагноз."
                : "Ответ сохранён. Закреплённого врача у вас нет — при необходимости обратитесь к специалисту или по номеру 112."}
          </p>
        </>
      ) : state.status !== "in_progress" ? (
        <>
          <p style={{ color: "#5a6478", fontSize: 14 }}>
            Несколько коротких вопросов о том, как вы чувствовали себя сегодня. Занимает около минуты.
          </p>
          <button onClick={() => run(api.startCheckin)} disabled={busy}>
            {busy ? "Открываем…" : "Начать чек-ин"}
          </button>
        </>
      ) : (
        <>
          <p style={{ color: "#5a6478", fontSize: 13 }}>
            Прогресс: {state.progress.answered} из {state.progress.planned} · на этом шаге{" "}
            {items.length} {items.length === 1 ? "пункт" : "пункта"}
          </p>
          {items.map((item) => (
            <div key={item.id} className="qa" data-item={item.id}>
              <p>{item.text}</p>
              <div className="row">
                {Array.from({ length: item.max_value + 1 }, (_, v) => v).map((v) => (
                  <button
                    key={v}
                    className={stepAnswers[item.id] === v ? "on" : ""}
                    aria-pressed={stepAnswers[item.id] === v}
                    onClick={() => setStepAnswers((a) => ({ ...a, [item.id]: v }))}
                  >
                    {item.emoji[v] ? `${item.emoji[v]} ` : ""}
                    {item.anchors[v] ?? String(v)}
                  </button>
                ))}
              </div>
            </div>
          ))}
          <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 10, flexWrap: "wrap" }}>
            <button onClick={submit} disabled={busy || !allAnswered}>
              {busy ? "Сохраняем…" : lastStep ? "Отправить" : "Далее"}
            </button>
            {!allAnswered && (
              <span style={{ color: "#5a6478", fontSize: 13 }}>
                Ответьте на пункты шага ({answeredInStep} из {items.length})
              </span>
            )}
          </div>
        </>
      )}
      {err && <p className="error">{err}</p>}
      <SafetyBlock safety={state.safety} notified={state.clinician_notified} />
    </>
  );
}

function TodayTab() {
  const [content, setContent] = useState("");
  const [mood, setMood] = useState<number | null>(null);
  const [sleep, setSleep] = useState(7);
  const [entriesToday, setEntriesToday] = useState<DiaryEntry[]>([]);
  const [status, setStatus] = useState<"taken" | "partial" | "missed" | null>(null);
  const [medication, setMedication] = useState("");
  const [adhNote, setAdhNote] = useState("");
  const [adhMeds, setAdhMeds] = useState<AdherenceSummary["active_medications"]>([]);
  const [adhMedId, setAdhMedId] = useState("");
  const [adhItems, setAdhItems] = useState<AdherenceRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState("");
  const [err, setErr] = useState("");

  const load = async () => {
    const [d, a] = await Promise.all([api.getDiary(), api.getMyAdherence()]);
    const ref = new Date();
    setEntriesToday(d.items.filter((e) => sameDay(e.created_at, ref)));
    setAdhMeds(a.active_medications);
    setAdhItems(a.items);
    setAdhMedId((prev) => prev || a.active_medications[0]?.id || "");
  };

  useEffect(() => {
    load().catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  }, []);

  const save = async () => {
    setErr("");
    setSaved("");
    setBusy(true);
    const parts: string[] = [];
    try {
      if (content.trim()) {
        await api.postDiary({ content, mood: mood ?? undefined, sleep_h: sleep, tags: [] });
        setContent("");
        setMood(null);
        parts.push("дневник");
      }
      const ref = new Date();
      const markedToday = adhItems.some(
        (r) => sameDay(r.created_at, ref) && (adhMedId ? r.medication_id === adhMedId : !r.medication_id),
      );
      if (!markedToday && status) {
        await api.recordAdherence({
          medication_id: adhMedId || undefined,
          medication: adhMedId ? undefined : medication.trim() || undefined,
          status,
          note: adhNote.trim() || undefined,
        });
        setMedication("");
        setAdhNote("");
        parts.push("приём лекарств");
      }
      await load();
      setSaved(parts.length > 0 ? `Сохранено: ${parts.join(" · ")}` : "Всё уже отмечено за сегодня");
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const markedTodayForSelected = adhItems.find((r) => {
    const ref = new Date();
    return sameDay(r.created_at, ref) && (adhMedId ? r.medication_id === adhMedId : !r.medication_id);
  });
  return (
    <div className="card">
      <h2>Сегодня</h2>
      <p style={{ color: "#5a6478", fontSize: 14 }}>
        {new Date().toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" })} — чек-ин,
        дневник и приём лекарств в одной форме.
      </p>

      <h3>Чек-ин</h3>
      <CheckinCard />

      <h3>Дневник</h3>
      <label htmlFor="today-diary">Как прошёл день?</label>
      <textarea id="today-diary" rows={3} value={content} onChange={(e) => setContent(e.target.value)} />
      <div className="two">
        <div>
          <label>Настроение</label>
          <div className="row mood">
            {MOOD_OPTIONS.map((option, v) => (
              <button
                key={v}
                className={mood === v ? "on" : ""}
                aria-pressed={mood === v}
                onClick={() => setMood(v)}
              >
                {option.emoji} {option.label}
              </button>
            ))}
          </div>
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
              <b>{moodText(e.mood, e.mood_scale)}</b> {e.content}
            </div>
          ))}
        </div>
      )}

      <h3>Приём лекарств</h3>
      {adhMeds.length > 0 && (
        <select value={adhMedId} onChange={(e) => setAdhMedId(e.target.value)}>
          {adhMeds.map((m) => (
            <option key={m.id} value={m.id}>
              {m.title}
              {m.times_per_day != null ? ` (${doseWord(m.times_per_day)})` : " (режим не задан)"}
            </option>
          ))}
        </select>
      )}
      {markedTodayForSelected ? (
        <p style={{ color: "#2e7d32", fontWeight: 600 }}>
          За сегодня отмечено: {ADH_LABELS[markedTodayForSelected.status] ?? markedTodayForSelected.status}
          {markedTodayForSelected.medication ? ` (${markedTodayForSelected.medication})` : ""}
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
          {adhMeds.length === 0 && (
            <input
              placeholder="Препарат (напр. Авиандр) — необязательно"
              value={medication}
              onChange={(e) => setMedication(e.target.value)}
            />
          )}
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
          <b>{moodText(entry.mood, entry.mood_scale)}</b> {entry.content}
          <div style={{ color: "#5a6478", fontSize: 12 }}>
            {new Date(entry.created_at).toLocaleString("ru-RU")}
            {entry.sleep_h != null ? ` · сон ${entry.sleep_h} ч` : ""}
          </div>
        </div>
      ))}
    </div>
  );
}

// FR-27: процент — от ожидаемых приёмов по назначенному режиму, а не от числа отметок.
const doseWord = (n: number) => `${n} ${n === 1 ? "раз" : "раза"} в день`;
const fmtScore = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(1));

function AdherenceTab() {
  const [data, setData] = useState<AdherenceSummary | null>(null);
  const [days, setDays] = useState(30);
  const [medId, setMedId] = useState("");
  const [status, setStatus] = useState<"taken" | "partial" | "missed" | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [saved, setSaved] = useState("");

  const load = async (n = days) => {
    const d = await api.getMyAdherence(n);
    setData(d);
    setMedId((prev) => prev || d.active_medications[0]?.id || "");
  };

  useEffect(() => {
    setErr("");
    setMedId("");
    load(days).catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days]);

  const mark = async () => {
    if (!status) return;
    setErr("");
    setSaved("");
    setBusy(true);
    try {
      await api.recordAdherence({
        medication_id: medId || undefined,
        status,
      });
      setStatus(null);
      setSaved("Отметка сохранена");
      await load();
    } catch (e: unknown) {
      setErr(e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const meds = data?.medications ?? [];
  const active = data?.active_medications ?? [];
  const selected = active.find((m) => m.id === medId);
  const summary = data?.summary;
  const items = data?.items ?? [];

  return (
    <div className="card">
      <h2>Приверженность лечению</h2>
      <p style={{ color: "#5a6478", fontSize: 14 }}>
        Процент считается от ожидаемых приёмов по назначенному режиму (сколько раз в день
        принимать), а не от числа отметок. Частичный приём = 0,5.
      </p>
      <div style={{ display: "flex", gap: 8, margin: "8px 0" }}>
        {[7, 30].map((n) => (
          <button key={n} className={days === n ? "active" : "ghost"} onClick={() => setDays(n)}>
            {n} дней
          </button>
        ))}
      </div>

      {err && <p className="error">{err}</p>}

      {summary && summary.expected > 0 ? (
        <p>
          <b>Приверженность по режиму: {summary.pct}%</b>{" "}
          <span style={{ color: "#5a6478", fontSize: 13 }}>
            (принято {fmtScore(summary.score)} из {summary.expected} ожидаемых приёмов)
          </span>
        </p>
      ) : (
        <p style={{ color: "#5a6478" }}>
          Расписание приёма не задано — процент не считается. Врач может указать режим
          в назначении.
        </p>
      )}

      {meds.map((m) => (
        <div key={m.id} className="msg">
          <b>{m.title}</b>
          {m.times_per_day != null
            ? ` · режим ${doseWord(m.times_per_day)}`
            : " · расписание не задано, процент не считается"}
          {m.times_per_day != null && (
            <div style={{ color: "#3d4657", fontSize: 13 }}>
              принято {fmtScore(m.score)} из {m.expected} ({m.pct}%) · режим {doseWord(m.times_per_day)}
              <span style={{ color: "#5a6478" }}>
                {" "}— принято {m.taken}, частично {m.partial}, пропущено {m.missed}
              </span>
            </div>
          )}
        </div>
      ))}
      {meds.length === 0 && active.length > 0 && (
        <p style={{ color: "#5a6478" }}>По активным назначениям пока нет ожидаемых приёмов в окне.</p>
      )}

      <h3>Отметить приём</h3>
      {active.length === 0 ? (
        <p style={{ color: "#5a6478" }}>Активных назначений нет — врач ещё не назначил препарат.</p>
      ) : (
        <>
          <select value={medId} onChange={(e) => setMedId(e.target.value)}>
            {active.map((m) => (
              <option key={m.id} value={m.id}>
                {m.title}
                {m.times_per_day != null ? ` (${doseWord(m.times_per_day)})` : " (режим не задан)"}
              </option>
            ))}
          </select>
          <div style={{ display: "flex", gap: 8, margin: "8px 0" }}>
            {(["taken", "partial", "missed"] as const).map((s) => (
              <button key={s} className={status === s ? "active" : "ghost"} onClick={() => setStatus(s)}>
                {ADH_LABELS[s]}
              </button>
            ))}
          </div>
          {selected && selected.times_per_day == null && (
            <p style={{ color: "#5a6478", fontSize: 13 }}>
              У этого назначения режим не задан: отметка сохранится, но в процент не войдёт.
            </p>
          )}
          <button onClick={mark} disabled={busy || !status}>
            {busy ? "Сохраняем…" : "Сохранить отметку"}
          </button>
          {saved && <span style={{ color: "#2e7d32", fontSize: 13, marginLeft: 10 }}>{saved}</span>}
        </>
      )}

      {data && data.excluded.count > 0 && (
        <p style={{ color: "#5a6478", fontSize: 13, marginTop: 12 }}>
          Вне расчёта: {data.excluded.count} отметок — старые записи без привязки к назначению
          или назначения без заданного режима. Они видны в истории, но не входят в процент.
        </p>
      )}

      <div style={{ marginTop: 12 }}>
        {items.map((r) => (
          <div key={r.id} className="msg">
            <b>{ADH_LABELS[r.status] ?? r.status}</b>
            {r.medication ? ` · ${r.medication}` : ""}
            {r.medication_id ? "" : " · без привязки к назначению"}
            {r.note ? ` · ${r.note}` : ""}
            <div style={{ color: "#5a6478", fontSize: 12 }}>{new Date(r.created_at).toLocaleString("ru-RU")}</div>
          </div>
        ))}
        {items.length === 0 && <p style={{ color: "#5a6478" }}>Отметок пока нет.</p>}
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

// FR-22: свои контакты. Без активного согласия раздел недоступен (require_consent).
function ContactsTab() {
  const [phone, setPhone] = useState("");
  const [birthDate, setBirthDate] = useState("");
  const [age, setAge] = useState<number | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .myContacts()
      .then((c) => {
        setPhone(c.phone ?? "");
        setBirthDate(c.birth_date ?? "");
        setAge(c.age);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  const save = async () => {
    setError("");
    setSaved(false);
    try {
      const c = await api.myContactsUpdate({
        phone: phone.trim() === "" ? null : phone.trim(),
        birth_date: birthDate.trim() === "" ? null : birthDate.trim(),
      });
      setPhone(c.phone ?? "");
      setBirthDate(c.birth_date ?? "");
      setAge(c.age);
      setSaved(true);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  return (
    <div className="card">
      <h2>Контакты</h2>
      <p style={{ color: "#5a6478", fontSize: 14 }}>
        Телефон и дата рождения видны вашему лечащему врачу. Телефон хранится в зашифрованном виде,
        в списке пациентов показывается маской. При отзыве согласия контакты удаляются.
      </p>
      <div className="row" style={{ alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <label htmlFor="my-phone" style={{ fontSize: 14 }}>Телефон</label>
        <input
          id="my-phone"
          value={phone}
          placeholder="+7XXXXXXXXXX"
          onChange={(e) => setPhone(e.target.value)}
          style={{ maxWidth: 220 }}
        />
        <label htmlFor="my-birth" style={{ fontSize: 14 }}>Дата рождения</label>
        <input
          id="my-birth"
          type="date"
          value={birthDate}
          onChange={(e) => setBirthDate(e.target.value)}
          style={{ maxWidth: 180 }}
        />
        <button onClick={save}>Сохранить</button>
        {saved && <span style={{ color: "#2e7d32", fontSize: 13 }}>Сохранено</span>}
      </div>
      {age !== null && <p style={{ color: "#5a6478", fontSize: 13 }}>Возраст: {age}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}