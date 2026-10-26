import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  AppointmentChannel,
  ClinicalSession,
  MentalStatus,
  NeedsReview,
  ProtocolSections,
  ProtocolStatus,
  SessionDetail,
  SessionProtocol,
  TreatmentRow,
} from "../api";
import SessionAssistantCard from "./SessionAssistantCard";
import SessionDocumentCard from "./SessionDocumentCard";

/**
 * FR-39/40/41: панель приёма — ручной протокол (docs/18 §4.2, §4.4, §5), ИИ-ассистент
 * (§4.3), подпись/PDF/передача в ЕМИАС (§7, §8).
 *
 * Панель владеет редактором и слиянием черновика: ИИ никогда не затирает введённый
 * врачом текст — расхождения показываются отдельным блоком с выбором. Подпись и
 * доставка берутся из свежей детали сессии после каждой мутации, чтобы после новой
 * версии протокола не показывать старые квитанции.
 */

const CHANNEL_LABELS: Record<AppointmentChannel, string> = {
  in_person: "В кабинете",
  online: "Онлайн",
  phone: "Телефон",
};
const PROTOCOL_STATUS_LABELS: Record<ProtocolStatus, string> = {
  draft: "черновик",
  approved: "подтверждён",
  signed: "подписан",
  submitted: "передан в ЕМИАС",
  superseded: "заменён",
};
/** §5.2: потолки разделов (символов) — зеркало app/services/protocols.LIMITS. */
const LIMIT = {
  complaints: 4000,
  anamnesis_morbi: 8000,
  mental: 2000,
  somatic_status: 2000,
  recommendations: 4000,
  rationale: 2000,
  line: 500,
  treatmentMax: 20,
};
const MENTAL_FIELDS: { key: keyof MentalStatus; label: string }[] = [
  { key: "appearance_behavior", label: "Внешний вид и поведение" },
  { key: "consciousness_orientation", label: "Сознание и ориентировка" },
  { key: "perception", label: "Восприятие" },
  { key: "thinking_speech", label: "Мышление и речь" },
  { key: "emotions_volition", label: "Эмоции и воля" },
  { key: "criticism", label: "Критика" },
  { key: "suicidal_risk", label: "Суицидальный риск (обязательно)" },
];
const TIME_FMT = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
const START_FMT = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "long",
  hour: "2-digit",
  minute: "2-digit",
});

const errText = (e: unknown): string =>
  e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e);
/** §4.3: причины `needs_review` → подсказки врачу (код модели не показываем). */
const REVIEW_REASONS: Record<string, string> = {
  dosage_in_draft: "похоже на дозировку — проверьте вручную",
  clinical_risk: "оценка риска требует проверки врачом",
  unexpected_section: "модель вернула посторонний раздел — перенос отклонён",
};
const NARRATIVE_KEYS = ["complaints", "anamnesis_morbi", "somatic_status", "recommendations"] as const;
const DIAGNOSIS_KEYS = ["syndrome", "icd10", "rationale"] as const;

/** Поле редактора по пути из `needs_review` (`complaints`, `mental_status.criticism`, …). */
function fieldLabel(key: string): string {
  const mental = MENTAL_FIELDS.find((field) => `mental_status.${field.key}` === key);
  if (mental) return mental.label;
  const labels: Record<string, string> = {
    complaints: "Жалобы",
    anamnesis_morbi: "Анамнез",
    somatic_status: "Соматический статус",
    recommendations: "Рекомендации",
    "diagnosis.syndrome": "Синдром",
    "diagnosis.icd10": "МКБ-10",
    "diagnosis.rationale": "Обоснование диагноза",
    llm_output: "Посторонние ключи модели",
  };
  return labels[key] ?? key;
}

/** Только текстовые разделы: паспорт, диагноз и лечение ИИ не заполняет (§5.1). */
function draftKeys(draft: ProtocolSections): string[] {
  const keys: string[] = [];
  for (const name of NARRATIVE_KEYS) {
    if (typeof draft[name] === "string") keys.push(name);
  }
  for (const field of MENTAL_FIELDS) {
    if (typeof draft.mental_status?.[field.key] === "string") keys.push(`mental_status.${field.key}`);
  }
  for (const name of DIAGNOSIS_KEYS) {
    if (typeof draft.diagnosis?.[name] === "string") keys.push(`diagnosis.${name}`);
  }
  return keys;
}

function fieldValue(sections: ProtocolSections, key: string): string {
  if (key.startsWith("mental_status.")) {
    const name = key.slice("mental_status.".length) as keyof MentalStatus;
    return sections.mental_status[name] ?? "";
  }
  if (key.startsWith("diagnosis.")) {
    const name = key.slice("diagnosis.".length) as keyof ProtocolSections["diagnosis"];
    return sections.diagnosis[name] ?? "";
  }
  const value = (sections as unknown as Record<string, unknown>)[key];
  return typeof value === "string" ? value : "";
}

function withField(sections: ProtocolSections, key: string, value: string): ProtocolSections {
  if (key.startsWith("mental_status.")) {
    const name = key.slice("mental_status.".length) as keyof MentalStatus;
    return { ...sections, mental_status: { ...sections.mental_status, [name]: value } };
  }
  if (key.startsWith("diagnosis.")) {
    const name = key.slice("diagnosis.".length) as keyof ProtocolSections["diagnosis"];
    return { ...sections, diagnosis: { ...sections.diagnosis, [name]: value } };
  }
  return { ...sections, [key]: value };
}

/** Раздел → список причин проверки: бейдж ставится у самого поля редактора. */
function reviewByField(items: NeedsReview[]): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const item of items) {
    const key = item.key ?? item.section;
    const reason = REVIEW_REASONS[item.reason] ?? "проверьте после ИИ";
    (out[key] ??= []).push(reason);
  }
  return out;
}

interface DraftConflict {
  key: string;
  next: string;
}

/** Пустой протокол по §5.1: паспорт из сессии, остальное заполняет врач. */
function emptySections(session: ClinicalSession): ProtocolSections {
  return {
    passport: { visit_at: session.started_at, channel: session.channel, legal_basis: "" },
    complaints: "",
    anamnesis_morbi: "",
    mental_status: {
      appearance_behavior: "",
      consciousness_orientation: "",
      perception: "",
      thinking_speech: "",
      emotions_volition: "",
      criticism: "",
      suicidal_risk: "",
    },
    somatic_status: "",
    diagnosis: { syndrome: "", icd10: "", rationale: "" },
    treatment: [],
    recommendations: "",
  };
}

/** Черновик с сервера может не содержать ключей (ИИ заполняет подмножество) — домешиваем. */
function withDefaults(base: ProtocolSections, loaded: ProtocolSections): ProtocolSections {
  return {
    ...base,
    ...loaded,
    passport: { ...base.passport, ...(loaded.passport ?? {}) },
    mental_status: { ...base.mental_status, ...(loaded.mental_status ?? {}) },
    diagnosis: { ...base.diagnosis, ...(loaded.diagnosis ?? {}) },
    treatment: Array.isArray(loaded.treatment) ? loaded.treatment : [],
  };
}

/** Счётчик символов: показываем у предела (последние 10%), чтобы не шуметь. */
function Counter({ value, max }: { value: string; max: number }) {
  if (value.length < max * 0.9) return null;
  return (
    <span className={`small counter${value.length > max ? " over" : ""}`}>
      {value.length} / {max}
    </span>
  );
}

function NarrativeField({
  label,
  value,
  max,
  rows = 3,
  review,
  onChange,
}: {
  label: string;
  value: string;
  max: number;
  rows?: number;
  /** Причины из `needs_review` черновика ИИ: раздел проверяет врач (§4.3). */
  review?: string[];
  onChange: (value: string) => void;
}) {
  return (
    <div>
      <label>
        {label}
        <textarea
          rows={rows}
          value={value}
          maxLength={max}
          onChange={(e) => onChange(e.target.value)}
        />
      </label>
      <Counter value={value} max={max} />
      {review && review.length > 0 && (
        <p className="hint">
          <span className="badge YELLOW">проверить после ИИ</span> {review.join("; ")}
        </p>
      )}
    </div>
  );
}

export default function SessionPanel({
  session: initial,
  patientLabel,
  onClose,
}: {
  session: ClinicalSession;
  /** ФИО или email из ростера: у пациента без ФИО в сессии только `patient_fio = null`. */
  patientLabel: string;
  onClose: () => void;
}) {
  const [session, setSession] = useState(initial);
  const [protocol, setProtocol] = useState<SessionProtocol | null>(null);
  // Пустой протокол по §5.1 — база для домешивания: паспорт из сессии неизменен.
  const base = useMemo(() => emptySections(initial), [initial]);
  const [sections, setSections] = useState<ProtocolSections>(base);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  // Ключи строк лечения: `key={i}` после удаления из середины переиспользует DOM строки.
  const [rowKeys, setRowKeys] = useState<string[]>([]);
  const rowSeq = useRef(0);
  // §4.2: свежая деталь сессии — источник ai_status, подписи и доставки.
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [review, setReview] = useState<Record<string, string[]>>({});
  const [conflicts, setConflicts] = useState<DraftConflict[]>([]);

  const refresh = useCallback(async () => {
    const fresh = await api.getSession(session.id);
    setDetail(fresh);
    setSession(fresh);
    // Статус активной версии меняют подпись и передача — подтягиваем его в шапку и карточку
    // документа; чужую версию (врач уже правил) не трогаем.
    setProtocol((current) =>
      current && fresh.protocol && fresh.protocol.version === current.version
        ? { ...current, status: fresh.protocol.status }
        : current,
    );
    return fresh;
  }, [session.id]);

  useEffect(() => {
    let cancelled = false;
    api
      .getSession(session.id)
      .then((fresh) => {
        if (!cancelled) setDetail(fresh);
      })
      .catch(() => {
        // Деталь — вспомогательный блок: без неё ручной протокол работает, ошибка не нужна.
      });
    return () => {
      cancelled = true;
    };
  }, [session.id]);

  useEffect(() => {
    let cancelled = false;
    api
      .getProtocol(session.id)
      .then((p) => {
        if (cancelled) return;
        setProtocol(p);
        setSections(withDefaults(base, p.sections));
        setRowKeys((p.sections.treatment ?? []).map(() => `t${++rowSeq.current}`));
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        // 404 PROTOCOL_NOT_FOUND — протокола ещё нет: это пустой черновик, не ошибка.
        if (e instanceof ApiError && e.status === 404) return;
        setErr(errText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [session.id, base]);

  const locked = protocol !== null && protocol.status !== "draft";
  // Таймер приёма (§6): у завершённой сессии время считается один раз, у идущей — тикает.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (session.ended_at) return;
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [session.ended_at]);

  const elapsedSec = Math.max(
    0,
    Math.floor(
      ((session.ended_at ? new Date(session.ended_at).getTime() : now) -
        new Date(session.started_at).getTime()) /
        1000,
    ),
  );
  const duration =
    session.ended_at === null
      ? `идёт ${Math.floor(elapsedSec / 60)}:${String(elapsedSec % 60).padStart(2, "0")}`
      : `${Math.max(1, Math.round(elapsedSec / 60))} мин`;

  const save = async () => {
    setBusy(true);
    setErr("");
    setNote("");
    try {
      const code = sections.diagnosis.icd10.trim() || null;
      const p = await api.putProtocol(session.id, sections, code);
      setProtocol(p);
      setSections(withDefaults(base, p.sections));
      setConflicts([]);
      await refresh();
      setNote(`Черновик сохранён (версия ${p.version}).`);
    } catch (e: unknown) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const approve = async () => {
    setBusy(true);
    setErr("");
    setNote("");
    try {
      // Подтверждаем ровно то, что видит врач: approve уходит по последнему черновику,
      // поэтому несохранённые правки сначала сохраняем (§4.4 PUT → approve).
      await api.putProtocol(session.id, sections, sections.diagnosis.icd10.trim() || null);
      const p = await api.approveProtocol(session.id);
      setProtocol(p);
      setSections(withDefaults(base, p.sections));
      setConflicts([]);
      setReview({});
      await refresh();
      setNote("Протокол подтверждён.");
    } catch (e: unknown) {
      // 409 PROTOCOL_NOT_READY — список незаполненных разделов в detail.
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const finish = async () => {
    setBusy(true);
    setErr("");
    try {
      setSession(await api.finishSession(session.id));
      await refresh();
      setNote("Приём завершён, запись добавлена в карточку пациента.");
    } catch (e: unknown) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };

  /**
   * Слияние черновика ИИ (§4.3): пустые разделы заполняются, введённый текст не
   * перезаписывается — расхождения уходят в блок выбора над редактором.
   */
  const applyDraft = (draft: SessionProtocol) => {
    let merged = sections;
    const disputed: DraftConflict[] = [];
    for (const key of draftKeys(draft.sections)) {
      const proposed = fieldValue(draft.sections, key);
      if (!proposed.trim()) continue;
      const mine = fieldValue(sections, key);
      if (!mine.trim()) merged = withField(merged, key, proposed);
      else if (mine !== proposed) disputed.push({ key, next: proposed });
    }
    setSections(merged);
    setConflicts(disputed);
    setReview(reviewByField(draft.needs_review));
    setProtocol(draft);
    setErr("");
    setNote(
      disputed.length > 0
        ? `Черновик перенесён в редактор; ${disputed.length} раздел(ов) не перезаписаны — выберите ниже.`
        : "Черновик ИИ перенесён в редактор — проверьте разделы с бейджем и сохраните.",
    );
  };

  const replaceWithDraft = (conflict: DraftConflict) => {
    setSections((current) => withField(current, conflict.key, conflict.next));
    setConflicts((list) => list.filter((item) => item.key !== conflict.key));
  };

  const keepMine = (conflict: DraftConflict) =>
    setConflicts((list) => list.filter((item) => item.key !== conflict.key));

  const setTreatment = (i: number, key: keyof TreatmentRow, value: string) =>
    setSections((s) => ({
      ...s,
      treatment: s.treatment.map((row, idx) => (idx === i ? { ...row, [key]: value } : row)),
    }));

  return (
    <div>
      <div className="card">
        <div className="schedule-head">
          <h2>Приём: {patientLabel}</h2>
          <button className="ghost" onClick={onClose}>
            К расписанию
          </button>
        </div>
        <p className="small muted">
          {CHANNEL_LABELS[session.channel]} · начало {START_FMT.format(new Date(session.started_at))}{" "}
          · длительность {duration} · статус{" "}
          {session.status === "finished" ? "завершён" : "активен"} · протокол{" "}
          {PROTOCOL_STATUS_LABELS[protocol?.status ?? "draft"]}
          {protocol ? ` (v${protocol.version})` : ""}
        </p>
        <div className="row">
          <button type="button" onClick={finish} disabled={busy || session.status === "finished"}>
            Завершить приём
          </button>
        </div>
        {err && (
          <p className="error" role="alert">
            {err}
          </p>
        )}
        {note && !err && <p className="small muted">{note}</p>}
      </div>

      <SessionAssistantCard
        session={session}
        protocolLocked={locked}
        hasAudio={detail?.artifacts.some((artifact) => artifact.kind === "audio") ?? false}
        onRefresh={refresh}
        onDraft={applyDraft}
      />

      <div className="card">
        <h2>Протокол приёма</h2>
        <p className="small muted">
          Черновик заполняет врач; после подтверждения запись неизменяема (правка — новая версия
          после подписи).
        </p>

        {locked ? (
          <div>
            <p className="small muted">
              Подтверждён {protocol?.approved_at ? START_FMT.format(new Date(protocol.approved_at)) : ""}
              {protocol?.content_sha256 ? ` · хэш ${protocol.content_sha256.slice(0, 12)}…` : ""}
            </p>
            <dl className="protocol-view">
              <dt>Дата и время приёма</dt>
              <dd>{sections.passport.visit_at || "—"}</dd>
              <dt>Канал</dt>
              <dd>{CHANNEL_LABELS[sections.passport.channel as AppointmentChannel] ?? sections.passport.channel}</dd>
              <dt>Правовое основание</dt>
              <dd>{sections.passport.legal_basis || "—"}</dd>
              <dt>Жалобы</dt>
              <dd>{sections.complaints || "—"}</dd>
              <dt>Анамнез заболевания</dt>
              <dd>{sections.anamnesis_morbi || "—"}</dd>
              {MENTAL_FIELDS.map((f) => (
                <div key={f.key}>
                  <dt>{f.label}</dt>
                  <dd>{sections.mental_status[f.key] || "—"}</dd>
                </div>
              ))}
              <dt>Соматический статус</dt>
              <dd>{sections.somatic_status || "—"}</dd>
              <dt>Синдром</dt>
              <dd>{sections.diagnosis.syndrome || "—"}</dd>
              <dt>МКБ-10</dt>
              <dd>{sections.diagnosis.icd10 || "—"}</dd>
              <dt>Обоснование диагноза</dt>
              <dd>{sections.diagnosis.rationale || "—"}</dd>
              <dt>Лечение</dt>
              <dd>
                {sections.treatment.length === 0
                  ? "—"
                  : sections.treatment.map((row, i) => (
                      <div key={i}>
                        {row.drug} {row.dose} {row.schedule} {row.duration}
                      </div>
                    ))}
              </dd>
              <dt>Рекомендации</dt>
              <dd>{sections.recommendations || "—"}</dd>
            </dl>
          </div>
        ) : (
          <div>
            {conflicts.length > 0 && (
              <div className="alert-warn">
                <p>
                  Черновик ИИ не перезаписал введённый текст: по каждому разделу ниже выберите
                  вариант.
                </p>
                {conflicts.map((conflict) => (
                  <div className="row" key={conflict.key}>
                    <span className="small">{fieldLabel(conflict.key)}</span>
                    <button type="button" className="ghost" onClick={() => replaceWithDraft(conflict)}>
                      Заменить на текст ИИ
                    </button>
                    <button type="button" className="ghost" onClick={() => keepMine(conflict)}>
                      Оставить моё
                    </button>
                  </div>
                ))}
              </div>
            )}

            <h3>Паспорт</h3>
            <div className="row">
              <div>
                <span className="small muted">Дата и время приёма (из сессии)</span>
                <p className="readonly-value">{sections.passport.visit_at}</p>
              </div>
              <div>
                <span className="small muted">Канал (из сессии)</span>
                <p className="readonly-value">
                  {CHANNEL_LABELS[sections.passport.channel as AppointmentChannel] ??
                    sections.passport.channel}
                </p>
              </div>
            </div>
            <NarrativeField
              label="Правовое основание (ИДС)"
              value={sections.passport.legal_basis}
              max={LIMIT.line}
              rows={1}
              onChange={(v) => setSections((s) => ({ ...s, passport: { ...s.passport, legal_basis: v } }))}
            />

            <h3>Жалобы</h3>
            <NarrativeField
              label="Жалобы"
              value={sections.complaints}
              max={LIMIT.complaints}
              review={review["complaints"]}
              onChange={(v) => setSections((s) => ({ ...s, complaints: v }))}
            />

            <h3>Анамнез заболевания</h3>
            <NarrativeField
              label="Anamnesis morbi"
              value={sections.anamnesis_morbi}
              max={LIMIT.anamnesis_morbi}
              rows={4}
              review={review["anamnesis_morbi"]}
              onChange={(v) => setSections((s) => ({ ...s, anamnesis_morbi: v }))}
            />

            <h3>Психический статус</h3>
            {MENTAL_FIELDS.map((f) => (
              <NarrativeField
                key={f.key}
                label={f.label}
                value={sections.mental_status[f.key]}
                max={LIMIT.mental}
                review={review[`mental_status.${String(f.key)}`]}
                onChange={(v) =>
                  setSections((s) => ({ ...s, mental_status: { ...s.mental_status, [f.key]: v } }))
                }
              />
            ))}

            <h3>Соматический статус</h3>
            <NarrativeField
              label="Соматический статус"
              value={sections.somatic_status}
              max={LIMIT.somatic_status}
              review={review["somatic_status"]}
              onChange={(v) => setSections((s) => ({ ...s, somatic_status: v }))}
            />

            <h3>Диагноз</h3>
            <div className="row">
              <label>
                Синдром
                <input
                  value={sections.diagnosis.syndrome}
                  maxLength={LIMIT.line}
                  onChange={(e) =>
                    setSections((s) => ({
                      ...s,
                      diagnosis: { ...s.diagnosis, syndrome: e.target.value },
                    }))
                  }
                />
              </label>
              <label>
                МКБ-10
                <input
                  value={sections.diagnosis.icd10}
                  maxLength={10}
                  placeholder="F41.1"
                  onChange={(e) =>
                    setSections((s) => ({
                      ...s,
                      diagnosis: { ...s.diagnosis, icd10: e.target.value.toUpperCase() },
                    }))
                  }
                />
              </label>
            </div>
            <NarrativeField
              label="Обоснование диагноза"
              value={sections.diagnosis.rationale}
              max={LIMIT.rationale}
              review={review["diagnosis.rationale"]}
              onChange={(v) => setSections((s) => ({ ...s, diagnosis: { ...s.diagnosis, rationale: v } }))}
            />

            <h3>Лечение</h3>
            {sections.treatment.length === 0 && <p className="small muted">Позиций нет.</p>}
            {sections.treatment.map((row, i) => (
              <div className="row" key={rowKeys[i] ?? `t${i}`}>
                <label>
                  Препарат
                  <input
                    value={row.drug}
                    maxLength={LIMIT.line}
                    onChange={(e) => setTreatment(i, "drug", e.target.value)}
                  />
                </label>
                <label>
                  Доза
                  <input
                    value={row.dose}
                    maxLength={LIMIT.line}
                    onChange={(e) => setTreatment(i, "dose", e.target.value)}
                  />
                </label>
                <label>
                  Схема
                  <input
                    value={row.schedule}
                    maxLength={LIMIT.line}
                    onChange={(e) => setTreatment(i, "schedule", e.target.value)}
                  />
                </label>
                <label>
                  Длительность
                  <input
                    value={row.duration}
                    maxLength={LIMIT.line}
                    onChange={(e) => setTreatment(i, "duration", e.target.value)}
                  />
                </label>
                <button
                  type="button"
                  className="ghost"
                  onClick={() => {
                    setSections((s) => ({
                      ...s,
                      treatment: s.treatment.filter((_, idx) => idx !== i),
                    }));
                    setRowKeys((k) => k.filter((_, idx) => idx !== i));
                  }}
                >
                  Убрать
                </button>
              </div>
            ))}
            <button
              type="button"
              className="ghost"
              disabled={sections.treatment.length >= LIMIT.treatmentMax}
              onClick={() => {
                setSections((s) => ({
                  ...s,
                  treatment: [...s.treatment, { drug: "", dose: "", schedule: "", duration: "" }],
                }));
                setRowKeys((k) => [...k, `t${++rowSeq.current}`]);
              }}
            >
              Добавить позицию
            </button>

            <h3>Рекомендации</h3>
            <NarrativeField
              label="Рекомендации"
              value={sections.recommendations}
              max={LIMIT.recommendations}
              review={review["recommendations"]}
              onChange={(v) => setSections((s) => ({ ...s, recommendations: v }))}
            />

            <div className="row" style={{ marginTop: 12 }}>
              <button type="button" onClick={save} disabled={busy}>
                Сохранить черновик
              </button>
              <button type="button" onClick={approve} disabled={busy}>
                Подтвердить протокол
              </button>
            </div>
          </div>
        )}
      </div>

      <SessionDocumentCard
        sessionId={session.id}
        protocol={protocol}
        signature={detail?.signature ?? null}
        delivery={detail?.delivery ?? null}
        onRefresh={refresh}
      />
    </div>
  );
}
