import { useCallback, useEffect, useState } from "react";
import {
  AdherenceRecord,
  AdherenceSummary,
  api,
  ApiError,
  Assessment,
  CheckinHistoryPoint,
  CheckinState,
  ChatMessage,
  Consent,
  DiaryEntry,
  MoodScale,
  MyAdverseEvent,
  MyDrugReference,
  NotificationItem,
  PartialKind,
  PatientContacts,
  Questionnaire,
  Timeline,
} from "../api";

type Tab =
  | "today"
  | "screening"
  | "diary"
  | "trend"
  | "adherence"
  | "adverse"
  | "drugs"
  | "account";

// FR-33: «Сегодня» — домашний экран (чек-ин + дневник + приём лекарств).
// Меню «3 полоски»: четыре пункта из ТЗ плюс возврат на главную.
const MENU: { id: Tab; label: string }[] = [
  { id: "today", label: "Сегодня" },
  { id: "diary", label: "Дневник" },
  { id: "trend", label: "Мой тренд" },
  { id: "adverse", label: "Побочные эффекты" },
  { id: "drugs", label: "Справочник" },
];

export default function PatientView({ onLogout }: { onLogout?: () => void }) {
  const [consent, setConsent] = useState<Consent | null>(null);
  const [consentChecked, setConsentChecked] = useState(false);
  const [tab, setTab] = useState<Tab>("today");
  const [menuOpen, setMenuOpen] = useState(false);

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

  const go = (id: Tab) => {
    setTab(id);
    setMenuOpen(false);
  };

  return (
    <div className="patient-view">
      <div className="patient-bar">
        <button
          className="ghost patient-burger"
          aria-label="Меню"
          aria-expanded={menuOpen}
          aria-controls="patient-menu"
          onClick={() => setMenuOpen((open) => !open)}
        >
          <span aria-hidden="true">☰</span> Меню
        </button>
        <span className="patient-bar-spacer" />
        <button className="ghost" aria-current={tab === "account" ? "page" : undefined} onClick={() => go("account")}>
          Личный кабинет
        </button>
      </div>
      {menuOpen && (
        <nav id="patient-menu" className="patient-menu" aria-label="Меню пациента">
          {MENU.map((item) => (
            <button
              key={item.id}
              className={tab === item.id ? "active" : ""}
              aria-current={tab === item.id ? "page" : undefined}
              onClick={() => go(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>
      )}
      {tab === "today" && (
        <>
          <div className="alert-info row">
            <span style={{ fontSize: 13 }}>Согласие активно (v{consent.version}, от {consent.signed_at.slice(0, 10)})</span>
          </div>
          <TodayTab onOpen={go} />
        </>
      )}
      {tab === "screening" && <ScreeningTab />}
      {tab === "diary" && <DiaryTab />}
      {tab === "adherence" && <AdherenceTab />}
      {tab === "trend" && <TrendTab />}
      {tab === "adverse" && <AdverseTab />}
      {tab === "drugs" && <DrugReferenceTab />}
      {tab === "account" && (
        <AccountScreen consent={consent} onRevoke={revokeConsent} onLogout={onLogout} />
      )}
      <ChatPanel />
    </div>
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

  // FR-34: пункт 9 пациенту не задаётся — помечаем, что риск по нему не оценён.
  const notAssessed = (result?.risk_flags ?? []).filter((f) => f.kind === "NOT_ASSESSED");

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
        {notAssessed.length > 0 && (
          <div className="alert-info" style={{ marginTop: 12 }} role="status">
            <b>Пункт 9 не задавался.</b> Сумма посчитана по 8 пунктам, пороги те же, что у полной
            шкалы (5 / 10 / 15 / 20), максимум — 24. Риск по этому пункту <b>не оценён</b>: это не
            значит, что его нет. О риске спрашивает врач и ежедневный чек-ин.
          </div>
        )}
        {result.escalation && (
          <div className="alert-danger" style={{ marginTop: 12 }}>
            <b>Спасибо, что ответили.</b> Результат сохранён — врач увидит его в вашей карте. Если
            вам сейчас тяжело, позвоните:
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

const ADH_LABELS: Record<string, string> = {
  taken: "Принял",
  partial: "Частично",
  missed: "Не принял",
  not_scheduled: "Не назначено сегодня",
};
type AdherenceStatus = "taken" | "partial" | "missed" | "not_scheduled";
const ADH_STATUSES: AdherenceStatus[] = ["taken", "partial", "missed", "not_scheduled"];

// FR-27: уточнение «частично» — пациент выбирает конкретную долю, а не 0,5 вслепую.
const PARTIAL_GROUPS: { title: string; options: { kind: PartialKind; label: string }[] }[] = [
  {
    title: "Часть таблетки",
    options: [
      { kind: "quarter", label: "1/4 таблетки" },
      { kind: "half", label: "1/2 таблетки" },
      { kind: "three_quarters", label: "3/4 таблетки" },
    ],
  },
  {
    title: "Часть доз",
    options: [
      { kind: "one_of_two", label: "1 из 2 доз" },
      { kind: "two_of_three", label: "2 из 3 доз" },
    ],
  },
  {
    title: "Другое",
    options: [
      { kind: "less_than_prescribed", label: "Принял меньше, чем назначено" },
      { kind: "self_changed", label: "Изменил дозу сам" },
    ],
  },
];
const partialKindLabel = (kind: string | null) => {
  if (!kind) return "";
  for (const group of PARTIAL_GROUPS) {
    const found = group.options.find((o) => o.kind === kind);
    if (found) return found.label;
  }
  return kind;
};
// Крупные кнопки-варианты: доступная отметка без слайдеров и эмодзи.
const ADH_BUTTON_STYLE = { padding: "10px 14px", fontSize: 15, minHeight: 44 };

function PartialPicker({
  value, onChange,
}: { value: PartialKind | null; onChange: (kind: PartialKind) => void }) {
  return (
    <fieldset style={{ border: "1px solid #d5dae4", borderRadius: 8, padding: 10, margin: "8px 0" }}>
      <legend style={{ fontSize: 14 }}>Сколько принято</legend>
      {PARTIAL_GROUPS.map((group) => (
        <div
          key={group.title}
          role="group"
          aria-label={group.title}
          style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 8 }}
        >
          <span style={{ fontSize: 13, color: "#5a6478", minWidth: 110 }}>{group.title}</span>
          {group.options.map((option) => (
            <button
              key={option.kind}
              type="button"
              className={value === option.kind ? "active" : "ghost"}
              aria-pressed={value === option.kind}
              style={ADH_BUTTON_STYLE}
              onClick={() => onChange(option.kind)}
            >
              {option.label}
            </button>
          ))}
        </div>
      ))}
    </fieldset>
  );
}

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
};

// Завершённый чек-ин без дополнительной оценки риска (safety = none | unassessed):
// спокойный текст без тревоги и без обещаний.
const CHECKIN_DONE_NONE = [
  "Дополнительная оценка сегодня не потребовалась",
  "По вашим сегодняшним ответам не было необходимости проводить дополнительную оценку состояния.",
  "Это не означает, что все возможные риски исключены. Если вы почувствуете, что вам становится тяжело справляться самостоятельно, обратитесь к специалисту.",
  "Если вам нужна срочная помощь, позвоните по номеру 112 или обратитесь в ближайшее отделение экстренной помощи.",
  "Ваши ответы сохранены.",
];

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

  // HR4 — только на шаге безопасности (docs/16 §2); в ядре его больше нет.
  const items = state.step.items.filter(
    (i) => i.id !== "HR4" || state.phase === "safety",
  );
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
          {state.safety === "none" || state.safety === "unassessed" ? (
            CHECKIN_DONE_NONE.map((p) => (
              <p key={p} style={{ color: "#5a6478", fontSize: 13 }}>
                {p}
              </p>
            ))
          ) : (
            <p style={{ color: "#5a6478", fontSize: 13 }}>
              {state.clinician_notified
                ? "Ваши ответы за сегодня видит врач в карте. Это отдельные наблюдения и их динамика, а не диагноз."
                : "Ответ сохранён. Закреплённого врача у вас нет — при необходимости обратитесь к специалисту или по номеру 112."}
            </p>
          )}
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
      {/* Плашку показываем только после завершения или при сигнале/разборе:
          незавершённый чек-ин не должен объявлять риск «неоценённым» раньше времени. */}
      {(state.status === "completed" ||
        state.safety === "signal" ||
        state.safety === "review") && (
        <SafetyBlock safety={state.safety} notified={state.clinician_notified} />
      )}
    </>
  );
}

function TodayTab({ onOpen }: { onOpen: (tab: Tab) => void }) {
  const [content, setContent] = useState("");
  const [mood, setMood] = useState<number | null>(null);
  const [sleep, setSleep] = useState(7);
  const [entriesToday, setEntriesToday] = useState<DiaryEntry[]>([]);
  const [status, setStatus] = useState<AdherenceStatus | null>(null);
  const [partialKind, setPartialKind] = useState<PartialKind | null>(null);
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

  // Приёмов в день может быть несколько (FR-27): каждый отмечается отдельной
  // записью прямо здесь, без перехода в историю. Считаем только отметки
  // выбранного назначения; «не назначено сегодня» закрывает день целиком и
  // приёмом не считается. Режим не задан (или отметка без назначения) —
  // допускаем одну отметку в день, как было до появления режимов.
  const todayRef = new Date();
  const todayForSelected = adhItems.filter((r) =>
    sameDay(r.created_at, todayRef) && (adhMedId ? r.medication_id === adhMedId : !r.medication_id),
  );
  const intakesExpected = adhMeds.find((m) => m.id === adhMedId)?.times_per_day ?? 1;
  const dayNotScheduled = todayForSelected.some((r) => r.status === "not_scheduled");
  const intakesMarked = todayForSelected.filter((r) => r.status !== "not_scheduled").length;
  const intakesLeft = Math.max(0, intakesExpected - intakesMarked);
  const intakesComplete = dayNotScheduled || intakesLeft === 0;

  const save = async () => {
    if (status === "partial" && !partialKind) {
      setErr("Укажите, сколько принято: часть таблетки, часть доз или другое.");
      return;
    }
    setErr("");
    setSaved("");
    setBusy(true);
    const parts: string[] = [];
    try {
      if (content.trim()) {
        await api.postDiary({ content, mood: mood ?? undefined, sleep_h: sleep, tags: [] });
        setContent("");
        // Выбор настроения оставляем: пользователь видит, что именно сохранилось.
        parts.push("дневник");
      }
      // Каждый клик — новая запись приёма, пока не набрался режим: прежнюю
      // отметку не перезаписываем, иначе второй приём за день терялся бы.
      if (status && !intakesComplete) {
        await api.recordAdherence({
          medication_id: adhMedId || undefined,
          medication: adhMedId ? undefined : medication.trim() || undefined,
          status,
          partial_kind: status === "partial" ? partialKind ?? undefined : undefined,
          note: adhNote.trim() || undefined,
        });
        setMedication("");
        setAdhNote("");
        setStatus(null);
        setPartialKind(null);
        parts.push("приём лекарств");
      }
      await load();
      const savedLabel =
        parts.length > 0
          ? `Сохранено: ${parts.join(" · ")}`
          : status
            ? `За сегодня отмечено ${intakesMarked} из ${intakesExpected} ` +
              `${intakePlural(intakesExpected, true)} — больше отметок не нужно`
            : "Нечего сохранять: отметьте приём или заполните дневник";
      setSaved(savedLabel);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <HomeHead onOpen={onOpen} />
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
            {/* FR-33: повторный клик снимает выбор — настроение необязательно. */}
            {MOOD_OPTIONS.map((option, v) => (
              <button
                key={v}
                className={mood === v ? "on" : ""}
                aria-pressed={mood === v}
                onClick={() => setMood((current) => (current === v ? null : v))}
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

      <div className="row" style={{ alignItems: "baseline" }}>
        <h3 style={{ marginRight: 8 }}>Приём лекарств</h3>
        <button className="ghost" onClick={() => onOpen("adherence")}>
          История приёма
        </button>
      </div>
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
      {intakesComplete ? (
        <>
          <p style={{ color: "#2e7d32", fontWeight: 600 }}>
            {dayNotScheduled
              ? "Сегодня не назначено — приёмы отмечать не нужно"
              : `За сегодня отмечено ${intakesMarked} из ${intakesExpected} ` +
                intakePlural(intakesExpected, true)}
          </p>
          {todayForSelected.map((r) => (
            <p key={r.id} style={{ color: "#5a6478", fontSize: 13 }}>
              {new Date(r.created_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })} ·{" "}
              <b>{ADH_LABELS[r.status] ?? r.status}</b>
              {r.status === "partial" && r.partial_kind ? ` — ${partialKindLabel(r.partial_kind)}` : ""}
              {r.medication ? ` (${r.medication})` : ""}
              {r.note ? ` · ${r.note}` : ""}
            </p>
          ))}
          <p style={{ color: "#5a6478", fontSize: 13 }}>
            Ошиблись с отметкой? Исправить можно в истории приёма.
          </p>
        </>
      ) : (
        <>
          {intakesMarked > 0 && (
            <p style={{ color: "#2e7d32", fontWeight: 600 }}>
              Отмечено {intakesMarked} из {intakesExpected} {intakePlural(intakesExpected, true)} за сегодня ·
              осталось отметить ещё {intakesLeft} {intakePlural(intakesLeft)}
            </p>
          )}
          <div
            role="group"
            aria-label="Отметка приёма"
            style={{ display: "flex", gap: 8, margin: "8px 0", flexWrap: "wrap" }}
          >
            {ADH_STATUSES.map((s) => (
              <button
                key={s}
                className={status === s ? "active" : "ghost"}
                aria-pressed={status === s}
                style={ADH_BUTTON_STYLE}
                onClick={() => {
                  setStatus(s);
                  if (s !== "partial") setPartialKind(null);
                }}
              >
                {ADH_LABELS[s]}
              </button>
            ))}
          </div>
          {status === "partial" && <PartialPicker value={partialKind} onChange={setPartialKind} />}
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
    </>
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
// Числительные для копий о приёмах: «1 приём», «2 приёма», «5 приёмов».
// После «из» нужен родительный падеж: «из 1 приёма», «из 2 приёмов».
const intakePlural = (n: number, genitive = false) => {
  const tail = n % 100;
  if (tail >= 11 && tail <= 14) return "приёмов";
  const last = n % 10;
  if (last === 1) return genitive ? "приёма" : "приём";
  if (last >= 2 && last <= 4) return genitive ? "приёмов" : "приёма";
  return "приёмов";
};
const fmtScore = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(1));

function AdherenceTab() {
  const [data, setData] = useState<AdherenceSummary | null>(null);
  const [days, setDays] = useState(30);
  const [medId, setMedId] = useState("");
  const [status, setStatus] = useState<AdherenceStatus | null>(null);
  const [partialKind, setPartialKind] = useState<PartialKind | null>(null);
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
    if (status === "partial" && !partialKind) {
      setErr("Укажите, сколько принято: часть таблетки, часть доз или другое.");
      return;
    }
    setErr("");
    setSaved("");
    setBusy(true);
    try {
      await api.recordAdherence({
        medication_id: medId || undefined,
        status,
        partial_kind: status === "partial" ? partialKind ?? undefined : undefined,
      });
      setStatus(null);
      setPartialKind(null);
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
        принимать), а не от числа отметок. Доля «частично» зависит от уточнения:
        1/4 = 0,25 · 1/2 = 0,5 · 3/4 = 0,75 · 1 из 2 = 0,5 · 2 из 3 ≈ 0,67.
        «Не назначено сегодня» в числитель и знаменатель не входит.
      </p>
      <div style={{ display: "flex", gap: 8, margin: "8px 0" }}>
        {[7, 30].map((n) => (
          <button key={n} className={days === n ? "active" : "ghost"} onClick={() => setDays(n)}>
            за последние {n} дней
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
                {" "}— принято {m.taken}, частично {m.partial}, пропущено {m.missed},
                не назначено {m.not_scheduled}
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
          <div
            role="group"
            aria-label="Отметка приёма"
            style={{ display: "flex", gap: 8, margin: "8px 0", flexWrap: "wrap" }}
          >
            {ADH_STATUSES.map((s) => (
              <button
                key={s}
                className={status === s ? "active" : "ghost"}
                aria-pressed={status === s}
                style={ADH_BUTTON_STYLE}
                onClick={() => {
                  setStatus(s);
                  if (s !== "partial") setPartialKind(null);
                }}
              >
                {ADH_LABELS[s]}
              </button>
            ))}
          </div>
          {status === "partial" && <PartialPicker value={partialKind} onChange={setPartialKind} />}
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
            {r.status === "partial" && r.partial_kind ? ` — ${partialKindLabel(r.partial_kind)}` : ""}
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

// FR-34 C: чат с врачом — пассивная плашка в правом нижнем углу, не отдельный раздел.
function ChatTab() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [alert, setAlert] = useState(false);
  const [crisis, setCrisis] = useState(false);
  const [quiet, setQuiet] = useState(false);
  const [error, setError] = useState("");

  const load = async () => {
    setMessages((await api.getChat()).items);
  };
  useEffect(() => {
    load().catch((e: unknown) => setError(e instanceof ApiError ? e.detail : String(e)));
    // Номера помощи не зависят от лимита автоответа: при последней RED-оценке
    // блок показывается и тогда, когда сервер вернул reply: null.
    api
      .getTimeline(90)
      .then((t) => setCrisis(t.runs.length > 0 && t.runs[t.runs.length - 1].triage === "RED"))
      .catch(() => undefined);
  }, []);

  const send = async () => {
    if (!input.trim()) return;
    setError("");
    setQuiet(false);
    try {
      const reply = await api.postChat(input);
      setInput("");
      setAlert(Boolean(reply.reply?.help_resources.length));
      // Помощник отвечает не чаще раза в день: второе сообщение просто сохраняется.
      setQuiet(reply.reply === null);
      await load();
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  return (
    <div className="patient-chat-inner">
      <h2>Чат с врачом</h2>
      <div className="chat-messages" style={{ maxHeight: 260, overflowY: "auto", marginBottom: 10 }}>
        {messages.length === 0 && (
          <p style={{ color: "#5a6478", fontSize: 13 }}>
            Здесь можно написать врачу о самочувствии. Сообщение увидит врач в вашей карте.
          </p>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.sender === "SYSTEM" ? "system" : ""}`}>
            <b>{m.sender === "SYSTEM" ? "Помощник: " : m.sender === "CLINICIAN" ? "Врач: " : "Вы: "}</b>
            {m.content}
          </div>
        ))}
      </div>
      {(alert || crisis) && (
        <div className="alert-danger">
          Если сейчас тяжело — позвоните: 8-800-2000-122 (телефон доверия), 112 (экстренная помощь).
        </div>
      )}
      {quiet && (
        <p className="alert-info" style={{ fontSize: 13 }}>
          Помощник отвечает не чаще одного раза в день. Ваше сообщение сохранено — врач увидит его в карте.
        </p>
      )}
      {error && <p className="error">{error}</p>}
      <div className="row">
        <input
          aria-label="Сообщение врачу"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
        />
        <button onClick={send} disabled={!input.trim()}>
          Отправить
        </button>
      </div>
    </div>
  );
}

// FR-33 E: «Мой тренд» — три SVG-графика без зависимостей: скрининги по шкалам,
// благополучие по чек-инам и процент приверженности по дням.
function TrendTab() {
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [checkins, setCheckins] = useState<CheckinHistoryPoint[]>([]);
  const [adherence, setAdherence] = useState<AdherenceSummary | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([api.getTimeline(90), api.getCheckinHistory(90), api.getMyAdherence(90)])
      .then(([t, c, a]) => {
        setTimeline(t);
        setCheckins(c.items);
        setAdherence(a);
      })
      .catch((e: unknown) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  if (error) return <div className="card"><p className="error">{error}</p></div>;
  if (!timeline || !adherence) return <div className="card">Загрузка…</div>;

  // Одна линия на шкалу: верх графика приходит с сервера (`max_total`), потому
  // что у PHQ-8 24, у GAD-7 21, у HCL-32 32, у PSS-10 40, у HDRS-17 52.
  const byScale = new Map<string, { points: { label: string; value: number }[]; max: number }>();
  timeline.runs.forEach((run) => {
    const code = run.questionnaire_code ?? "шкала";
    const entry = byScale.get(code) ?? { points: [], max: run.max_total ?? 0 };
    entry.points.push({ label: run.computed_at.slice(0, 10), value: run.total_score });
    byScale.set(code, entry);
  });
  const scaleCharts = [...byScale.entries()].map(([code, { points, max }]) => ({
    code,
    points,
    // Неизвестная шкала (нет в core/compass/scales.py) — верх по наблюдениям.
    max: max > 0 ? max : Math.max(1, ...points.map((p) => p.value)),
  }));

  const adherenceByDay = new Map<string, { score: number; total: number }>();
  adherence.items.forEach((record) => {
    if (record.status === "not_scheduled") return;
    const day = record.created_at.slice(0, 10);
    const bucket = adherenceByDay.get(day) ?? { score: 0, total: 0 };
    bucket.total += 1;
    if (record.status === "taken") bucket.score += 1;
    else if (record.status === "partial") bucket.score += 0.5;
    adherenceByDay.set(day, bucket);
  });
  const adherencePoints = [...adherenceByDay.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, b]) => ({ label: day, value: Math.round((100 * b.score) / b.total) }));

  return (
    <>
      <div className="card">
        <h2>Динамика за 90 дней</h2>
        {timeline.runs.length === 0 && <p style={{ color: "#5a6478" }}>Пройдите первый скрининг, чтобы увидеть тренд.</p>}
        {scaleCharts.map((chart) => (
          <LineChart
            key={chart.code}
            title={`Скрининг ${chart.code}`}
            points={chart.points}
            max={chart.max}
            unit=" б."
            color="#2f6fed"
          />
        ))}
      </div>
      <div className="card">
        <h2>Чек-ины</h2>
        <p style={{ color: "#5a6478", fontSize: 13 }}>
          Средняя оценка благополучия по пунктам чек-ина: 0 — тяжело, 4 — хорошо. Это не диагноз,
          а динамика самонаблюдения.
        </p>
        <LineChart title="Благополучие по дням" points={checkins.map((c) => ({ label: c.date, value: c.wellbeing }))} max={4} unit="" color="#2e7d32" />
      </div>
      <div className="card">
        <h2>Приверженность</h2>
        <p style={{ color: "#5a6478", fontSize: 13 }}>
          Процент принятых лекарств среди отмеченных за день: принято — 100%, частично — 50%,
          пропущено — 0%. Дни без отметок и приёмы «вне расписания» не входят в расчёт.
        </p>
        <LineChart title="Приём лекарств, %" points={adherencePoints} max={100} unit="%" color="#7b5cd6" />
      </div>
    </>
  );
}

// Простая полилиния с точками и подписями: один компонент на все три графика.
function LineChart({
  title,
  points,
  max,
  unit,
  color,
}: {
  title: string;
  points: { label: string; value: number }[];
  max: number;
  unit: string;
  color: string;
}) {
  if (points.length < 2) {
    return (
      <div style={{ marginTop: 12 }}>
        <h3>{title}</h3>
        <small>Недостаточно данных (нужно 2+ точки).</small>
      </div>
    );
  }
  const w = 560;
  const h = 150;
  const pad = 30;
  const px = (i: number) => pad + (i * (w - pad * 2)) / (points.length - 1);
  const py = (v: number) => h - pad - (v * (h - pad * 2)) / max;
  const line = points.map((p, i) => `${px(i).toFixed(1)},${py(p.value).toFixed(1)}`).join(" ");
  const last = points[points.length - 1];
  return (
    <div style={{ marginTop: 12 }}>
      <h3>{title}</h3>
      <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: "auto" }} role="img" aria-label={`${title}: ${points.length} точек`}>
        <line x1={pad} y1={py(0)} x2={w - pad} y2={py(0)} stroke="#e2e6ee" />
        <line x1={pad} y1={py(max)} x2={w - pad} y2={py(max)} stroke="#e2e6ee" />
        <text x={4} y={py(max) + 4} fontSize={10} fill="#5a6478">{max}</text>
        <text x={4} y={py(0) + 4} fontSize={10} fill="#5a6478">0</text>
        <text x={pad} y={h - 6} fontSize={10} fill="#5a6478">{points[0].label}</text>
        <text x={w - pad} y={h - 6} fontSize={10} fill="#5a6478" textAnchor="end">{last.label}</text>
        <polyline points={line} fill="none" stroke={color} strokeWidth={2.5} strokeLinejoin="round" />
        {points.map((p, i) => (
          <circle key={`${p.label}-${i}`} cx={px(i)} cy={py(p.value)} r={3} fill={color}>
            <title>{`${p.label}: ${p.value}${unit}`}</title>
          </circle>
        ))}
      </svg>
      <small>
        Последнее: {last.value}{unit} · {last.label}
      </small>
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

// FR-34: пациент видит оценку врача по-русски; NULL — «не оценена» (бейдж не выводим).
const ADVERSE_SEVERITY_LABELS: Record<string, string> = {
  mild: "лёгкое",
  moderate: "умеренное",
  severe: "тяжёлое",
};

// FR-28/FR-34: пациент сообщает о нежелательном явлении, врач проверяет его в карточке.
// Тяжесть — врачебная оценка (в форме её нет), статус виден пациенту,
// врачебные заметки — нет. Форма не обещает мгновенного ответа.
function AdverseTab() {
  const [items, setItems] = useState<MyAdverseEvent[]>([]);
  const [active, setActive] = useState<{ id: string; title: string }[]>([]);
  const [text, setText] = useState("");
  const [onsetDate, setOnsetDate] = useState(new Date().toISOString().slice(0, 10));
  const [medId, setMedId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const load = async () => {
    const [events, adherence] = await Promise.all([
      api.myAdverseEvents(),
      api.getMyAdherence(14),
    ]);
    setItems(events.items);
    setActive(adherence.active_medications);
  };

  useEffect(() => {
    load().catch((e: unknown) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  const submit = async () => {
    if (!text.trim()) return;
    setError("");
    setSaved(false);
    setBusy(true);
    try {
      await api.reportAdverseEvent({
        event_text: text.trim(),
        onset_date: onsetDate,
        medication_id: medId || undefined,
      });
      setText("");
      setMedId("");
      setSaved(true);
      await load();
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <h2>Нежелательные явления</h2>
      <p style={{ color: "#5a6478", fontSize: 14 }}>
        Сообщите, если после приёма препарата появились необычные симптомы. Сообщение увидит ваш
        врач и отметит результат проверки; ответ приходит не мгновенно.
      </p>
      <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "flex-start" }}>
        <textarea
          value={text}
          maxLength={1000}
          rows={3}
          placeholder="Что случилось (до 1000 символов)"
          onChange={(e) => setText(e.target.value)}
          style={{ flex: "1 1 260px", minWidth: 220 }}
        />
        <label style={{ fontSize: 13 }}>
          Дата
          <input
            type="date"
            value={onsetDate}
            onChange={(e) => setOnsetDate(e.target.value)}
            style={{ maxWidth: 170 }}
          />
        </label>
        <label style={{ fontSize: 13 }}>
          Препарат
          <select value={medId} onChange={(e) => setMedId(e.target.value)}>
            <option value="">не указан</option>
            {active.map((m) => (
              <option key={m.id} value={m.id}>{m.title}</option>
            ))}
          </select>
        </label>
        <button onClick={submit} disabled={busy || !text.trim()}>
          {busy ? "Отправка…" : "Сообщить"}
        </button>
      </div>
      <p style={{ color: "#5a6478", fontSize: 13 }}>
        Тяжесть оценит врач при проверке. Если помощь нужна срочно, звоните 112.
      </p>
      {saved && <p style={{ color: "#2e7d32", fontSize: 13 }}>Сообщение отправлено врачу</p>}
      {error && <p className="error">{error}</p>}

      {items.length === 0 ? (
        <p style={{ color: "#5a6478", fontSize: 13 }}>Сообщений пока нет.</p>
      ) : (
        <div className="table-wrap">
          <table className="data-cards">
            <thead>
              <tr><th>Дата</th><th>Событие</th><th>Тяжесть</th><th>Статус проверки</th></tr>
            </thead>
            <tbody>
              {items.map((e) => (
                <tr key={e.id}>
                  <td data-label="Дата">
                    <div className="ae-meta">
                      <span className="ae-date">{e.onset_date}</span>
                      {e.medication_title && <span className="ae-drug">{e.medication_title}</span>}
                    </div>
                  </td>
                  <td data-label="Событие">{e.event_text}</td>
                  <td data-label="Тяжесть">
                    {e.severity ? (
                      <>
                        <span className={`badge ${e.severity === "severe" ? "RED" : e.severity === "moderate" ? "YELLOW" : "GREEN"}`}>
                          {ADVERSE_SEVERITY_LABELS[e.severity]}
                        </span>
                        {e.severity === "severe" && (
                          <div style={{ color: "#b00020", fontSize: 12, marginTop: 4 }}>
                            Срочно обратитесь за помощью: 112
                          </div>
                        )}
                      </>
                    ) : (
                      <span style={{ color: "#5a6478", fontSize: 13 }}>не оценена</span>
                    )}
                  </td>
                  <td data-label="Статус проверки">
                    <span className={`badge ${e.status === "confirmed" ? "GREEN" : e.status === "pending" ? "YELLOW" : ""}`}>
                      {e.status_label}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// FR-33: маска телефона для личного кабинета — полное значение показываем
// только в режиме правки (сервер отдаёт своё значение без маски).
function maskPhone(phone: string | null): string {
  if (!phone) return "не указан";
  const digits = phone.replace(/\D/g, "");
  if (digits.length < 7) return "•••";
  const rest = digits.slice(-7);
  return `+${digits[0]}•••${rest.slice(0, 3)}-${rest.slice(3, 5)}-${rest.slice(5)}`;
}

// Главный экран (FR-33 A): приветствие, дата и время, напоминания и баннер
// назначенного врачом скрининга. Ниже — чек-ин, дневник и приём лекарств.
type PrescribedScale = {
  code: string;
  title: string;
  prescribed_at: string | null;
  // Не null — по этому назначению уже есть завершённая оценка: баннер гаснет.
  completed_at: string | null;
};

function HomeHead({ onOpen }: { onOpen: (tab: Tab) => void }) {
  const [contacts, setContacts] = useState<PatientContacts | null>(null);
  const [scales, setScales] = useState<PrescribedScale[]>([]);
  const [checkinDone, setCheckinDone] = useState<boolean | null>(null);
  const [medsMarked, setMedsMarked] = useState<boolean | null>(null);
  const [unread, setUnread] = useState<NotificationItem[]>([]);

  useEffect(() => {
    api.myContacts().then(setContacts).catch(() => undefined);
    api
      .prescribedScales()
      .then((p) => setScales(p.scales))
      .catch(() => undefined);
    api
      .todayCheckin()
      .then((s) => setCheckinDone(s.status === "completed"))
      .catch(() => setCheckinDone(null));
    api
      .getMyAdherence(1)
      .then((a) => {
        const today = new Date();
        const marked = a.items.some((r) => sameDay(r.created_at, today));
        setMedsMarked(a.active_medications.length === 0 ? true : marked);
      })
      .catch(() => setMedsMarked(null));
    api
      .getNotifications()
      .then((n) => setUnread(n.items.filter((item) => !item.read).slice(0, 3)))
      .catch(() => undefined);
  }, []);

  const now = new Date();
  const reminders: string[] = [];
  // Только непройденные назначения: completed_at заполнен → скрининг уже пройден.
  const pendingScales = scales.filter((s) => !s.completed_at);
  if (checkinDone === false) reminders.push("Чек-ин на сегодня ещё не выполнен.");
  if (medsMarked === false) reminders.push("Приём лекарств на сегодня не отмечен.");
  if (pendingScales.length > 0) reminders.push(`Назначен скрининг: ${pendingScales.map((s) => s.title).join(", ")}.`);
  unread.forEach((n) => reminders.push(n.title));

  return (
    <section className="card patient-home">
      <h2>Здравствуйте{contacts?.fio ? `, ${contacts.fio}` : ""}!</h2>
      <p className="patient-now">
        {now.toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" })} ·{" "}
        {now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}
      </p>
      {pendingScales.length > 0 && (
        <div className="patient-banner">
          <b>Скрининг, назначенный врачом</b>
          <p>{pendingScales.map((s) => s.title).join(", ")}</p>
          <button onClick={() => onOpen("screening")}>Перейти к скринингу</button>
        </div>
      )}
      <h3>Напоминания</h3>
      {reminders.length === 0 ? (
        <p style={{ color: "#5a6478", fontSize: 14 }}>На сегодня всё отмечено.</p>
      ) : (
        <ul className="patient-reminders">
          {reminders.map((text) => (
            <li key={text}>{text}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

// FR-33 D: «Справочник» — только препараты из назначений пациента плюс
// карточка ГРЛС, если название нашлось в справочнике.
function DrugReferenceTab() {
  const [items, setItems] = useState<MyDrugReference[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .getMyDrugs()
      .then((r) => setItems(r.items))
      .catch((e: unknown) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="card">
      <h2>Справочник</h2>
      <p style={{ color: "#5a6478", fontSize: 14 }}>
        Карточки препаратов, которые назначил врач. Это справочная информация, а не назначение:
        дозу и схему не меняйте без врача.
      </p>
      {loading && <p>Загрузка…</p>}
      {error && <p className="error">{error}</p>}
      {!loading && items.length === 0 && <p style={{ color: "#5a6478" }}>Назначенных препаратов пока нет.</p>}
      {items.map((item) => (
        <section key={item.medication_id} className="patient-drug">
          <h3>{item.title}</h3>
          <p style={{ fontSize: 13, color: "#5a6478" }}>
            {item.dosage ? `${item.dosage} · ` : ""}
            {item.times_per_day ? `${item.times_per_day} раз(а) в день · ` : ""}
            {item.status_label}
          </p>
          {item.drug ? (
            <>
              <p>
                <b>{item.drug.trade_name}</b>
                {item.drug.inn ? ` · ${item.drug.inn}` : ""}
                {item.drug.form ? ` · ${item.drug.form}` : ""}
              </p>
              {item.drug.indications && <p><b>Показания.</b> {item.drug.indications}</p>}
              {item.drug.pharmacology && <p><b>Как действует.</b> {item.drug.pharmacology}</p>}
              {item.drug.contraindications && <p><b>Противопоказания.</b> {item.drug.contraindications}</p>}
              {item.drug.adverse_events && <p><b>Нежелательные явления.</b> {item.drug.adverse_events}</p>}
              {item.drug.special_notes && <p><b>Особые указания.</b> {item.drug.special_notes}</p>}
              {item.drug.source_urls.length > 0 && (
                <p style={{ fontSize: 13 }}>
                  Источники:{" "}
                  {item.drug.source_urls.map((url) => (
                    <a key={url} href={url} target="_blank" rel="noreferrer" style={{ marginRight: 8 }}>
                      ссылка
                    </a>
                  ))}
                </p>
              )}
            </>
          ) : (
            <p style={{ color: "#5a6478", fontSize: 13 }}>
              Карточки в справочнике нет — уточните информацию у врача или в инструкции к препарату.
            </p>
          )}
        </section>
      ))}
    </div>
  );
}

// FR-33 B: личный кабинет — ФИО, e-mail, телефон маской с правкой, дата рождения,
// правовые документы и выход.
function AccountScreen({
  consent,
  onRevoke,
  onLogout,
}: {
  consent: Consent;
  onRevoke: () => Promise<void>;
  onLogout?: () => void;
}) {
  const [contacts, setContacts] = useState<PatientContacts | null>(null);
  const [editing, setEditing] = useState(false);
  const [phone, setPhone] = useState("");
  const [birthDate, setBirthDate] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState("");

  const apply = (next: PatientContacts) => {
    setContacts(next);
    setPhone(next.phone ?? "");
    setBirthDate(next.birth_date ?? "");
  };

  useEffect(() => {
    api
      .myContacts()
      .then(apply)
      .catch((e: unknown) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  const save = async () => {
    setError("");
    setBusy(true);
    try {
      apply(await api.myContactsUpdate({ phone: phone.trim() || null, birth_date: birthDate || null }));
      setEditing(false);
      setSaved("Контактные данные сохранены");
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  // Отзыв согласия необратим: без подтверждения в диалоге запрос не уходит.
  useEffect(() => {
    if (!confirmRevoke) return;
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape" && !revoking) setConfirmRevoke(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirmRevoke, revoking]);

  const openRevoke = () => {
    setRevokeError("");
    setConfirmRevoke(true);
  };

  const doRevoke = async () => {
    setRevokeError("");
    setRevoking(true);
    try {
      await onRevoke();
    } catch (e: unknown) {
      setRevokeError(e instanceof ApiError ? e.detail : String(e));
      setRevoking(false);
    }
  };

  return (
    <div className="card">
      <h2>Личный кабинет</h2>
      {contacts ? (
        <dl className="patient-account">
          <dt>ФИО</dt>
          <dd>{contacts.fio || "не указано"}</dd>
          <dt>E-mail</dt>
          <dd>{contacts.email || "не указан"}</dd>
          <dt>Телефон</dt>
          <dd>
            {editing ? (
              <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+7 900 000-00-00" />
            ) : (
              <>
                {maskPhone(contacts.phone)}{" "}
                <button className="ghost" onClick={() => setEditing(true)}>
                  Изменить
                </button>
              </>
            )}
          </dd>
          <dt>Дата рождения</dt>
          <dd>
            {editing ? (
              <input type="date" value={birthDate} onChange={(e) => setBirthDate(e.target.value)} />
            ) : (
              contacts.birth_date || "не указана"
            )}
          </dd>
        </dl>
      ) : (
        !error && <p>Загрузка…</p>
      )}
      {editing && (
        <div className="row" style={{ marginTop: 10 }}>
          <button onClick={save} disabled={busy}>
            {busy ? "Сохраняем…" : "Сохранить"}
          </button>
          <button className="ghost" onClick={() => setEditing(false)} disabled={busy}>
            Отмена
          </button>
        </div>
      )}
      {saved && <p style={{ color: "#2e7d32", fontSize: 13 }}>{saved}</p>}
      {error && <p className="error">{error}</p>}
      <p style={{ color: "#5a6478", fontSize: 13 }}>
        Телефон хранится в зашифрованном виде; в списке пациентов он показан маской, лечащему
        врачу — полностью.
      </p>
      <h3>Документы</h3>
      <ul>
        <li>
          <a href="/legal/terms">Пользовательское соглашение</a>
        </li>
        <li>
          <a href="/legal/consent">Согласие на обработку персональных данных</a>
        </li>
      </ul>
      <h3>Согласие</h3>
      <p style={{ fontSize: 13 }}>
        Активно с {consent.signed_at.slice(0, 10)} (версия {consent.version}).
      </p>
      <div className="row">
        {onLogout && (
          <button className="ghost" onClick={onLogout}>
            Выйти
          </button>
        )}
      </div>
      <details className="danger-zone">
        <summary>Отзыв согласия</summary>
        <p className="danger-zone-note">
          Отзыв согласия необратим: аккаунт останется, но клинические данные и связь с врачом будут
          удалены.
        </p>
        <button type="button" className="link-danger" onClick={openRevoke}>
          Отозвать согласие
        </button>
      </details>
      {confirmRevoke && (
        <div
          className="modal-backdrop"
          role="presentation"
          onClick={() => { if (!revoking) setConfirmRevoke(false); }}
        >
          <div
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="revoke-consent-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="revoke-consent-title">Отозвать согласие?</h3>
            <p>
              Все данные будут безвозвратно удалены: дневник, чек-ины, результаты скринингов,
              переписка, назначения и отметки о приёме лекарств, приёмы с заметками врача,
              сообщения о побочных эффектах, напоминания, телефон и дата рождения.
              Врач больше не сможет вести вас в системе. Продолжить?
            </p>
            {revokeError && <p className="error">{revokeError}</p>}
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button
                className="ghost"
                autoFocus
                onClick={() => setConfirmRevoke(false)}
                disabled={revoking}
              >
                Отмена
              </button>
              <button className="danger" onClick={doRevoke} disabled={revoking}>
                {revoking ? "Удаляем…" : "Отозвать и удалить"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// Плашка чата: свёрнута в кнопку, разворачивается поверх контента и не мешает чтению.
function ChatPanel() {
  const [open, setOpen] = useState(false);
  return (
    <div className={`patient-chat ${open ? "open" : ""}`}>
      <button
        className="patient-chat-toggle"
        aria-expanded={open}
        aria-controls="patient-chat-body"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "Свернуть чат" : "Чат с врачом"}
      </button>
      {open && (
        <div id="patient-chat-body" className="patient-chat-body" role="dialog" aria-label="Чат с врачом">
          <ChatTab />
        </div>
      )}
    </div>
  );
}