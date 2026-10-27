import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  AppointmentChannel,
  ClinicalSession,
  MentalStatus,
  NeedsReview,
  ProtocolSections,
  ProtocolSource,
  ProtocolStatus,
  SessionChatProposal,
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

/**
 * Путь поля из Problem `fields` → якорь в редакторе. Ошибка обязана стоять у
 * самого поля: одна плашка в начале длинной формы не объясняет, что не так, и
 * врач видит «нажал — ничего не произошло» (жалоба с приёмки).
 */
export function fieldAnchorId(path: string): string {
  const row = /^(treatment|sources)\[(\d+)\]/.exec(path);
  if (row) return `pf-${row[1]}-${row[2]}`;
  if (path.startsWith("treatment")) return "pf-treatment";
  if (path === "sources" || path.startsWith("diagnosis.sources")) return "pf-sources";
  return `pf-${path.replace(/\./g, "-")}`;
}

/** Текст ошибки для поля: точный путь или любой вложенный в него. */
function errorFor(fieldErrors: Record<string, string>, path: string): string {
  if (fieldErrors[path]) return fieldErrors[path];
  const nested = Object.entries(fieldErrors).find(
    ([key]) => key.startsWith(`${path}.`) || key.startsWith(`${path}[`),
  );
  return nested ? nested[1] : "";
}
/** §4.3: причины `needs_review` → подсказки врачу (код модели не показываем). */
const REVIEW_REASONS: Record<string, string> = {
  dosage_in_draft: "похоже на дозировку — проверьте вручную",
  clinical_risk: "оценка риска требует проверки врачом",
  unexpected_section: "модель вернула посторонний раздел — перенос отклонён",
  source_missing: "источника нет в выдаче базы знаний — утверждение не подтверждено",
  drug_not_in_source: "препарата нет в указанном источнике — сверьте с базой знаний",
  dosage_not_in_source: "дозы нет в указанном источнике — сверьте с базой знаний",
  icd10_unverified: "код МКБ-10 не подтверждён источником — сверьте по справочнику",
  sources_unavailable: "база знаний недоступна — обоснований нет, проверьте вручную",
  invalid_item: "элемент модели не по схеме — перенос отклонён",
  too_many_items: "позиций больше лимита (20) — лишние не перенесены",
};
const NARRATIVE_KEYS = ["complaints", "anamnesis_morbi", "somatic_status", "recommendations"] as const;
const DIAGNOSIS_KEYS = ["syndrome", "icd10", "rationale"] as const;

/** Поле редактора по пути из `needs_review` (`complaints`, `mental_status.criticism`, …). */
function fieldLabel(key: string): string {
  const mental = MENTAL_FIELDS.find((field) => `mental_status.${field.key}` === key);
  if (mental) return mental.label;
  if (key.startsWith("treatment.")) {
    const index = Number(key.slice("treatment.".length));
    return `Лечение, позиция ${Number.isNaN(index) ? "?" : index + 1}`;
  }
  const labels: Record<string, string> = {
    complaints: "Жалобы",
    anamnesis_morbi: "Анамнез",
    somatic_status: "Соматический статус",
    recommendations: "Рекомендации",
    diagnosis: "Диагноз",
    treatment: "Лечение",
    "diagnosis.syndrome": "Синдром",
    "diagnosis.icd10": "МКБ-10",
    "diagnosis.rationale": "Обоснование диагноза",
    llm_output: "Посторонние ключи модели",
  };
  return labels[key] ?? key;
}

/** Текстовые разделы черновика: лечение (список) сливается отдельно, см. `applyDraft`. */
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
    const name = key.slice("diagnosis.".length) as (typeof DIAGNOSIS_KEYS)[number];
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
    const name = key.slice("diagnosis.".length) as (typeof DIAGNOSIS_KEYS)[number];
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

/** Текст серверной ошибки у конкретного поля (пусто — ничего не рисуем). */
function FieldError({ text }: { text?: string }) {
  if (!text) return null;
  return (
    <p className="field-error" role="alert">
      {text}
    </p>
  );
}

function NarrativeField({
  label,
  value,
  max,
  rows = 3,
  review,
  path,
  error,
  onChange,
  onTyped,
}: {
  label: string;
  value: string;
  max: number;
  rows?: number;
  /** Причины из `needs_review` черновика ИИ: раздел проверяет врач (§4.3). */
  review?: string[];
  /** Путь поля в документе (`mental_status.criticism`) — для якоря и ошибки. */
  path?: string;
  /** Ошибка сервера, относящаяся именно к этому полю. */
  error?: string;
  onChange: (value: string) => void;
  /** Врач начал править поле — подсказку снимаем, она уже неактуальна. */
  onTyped?: () => void;
}) {
  return (
    <div id={path ? fieldAnchorId(path) : undefined}>
      <label>
        {label}
        <textarea
          rows={rows}
          value={value}
          maxLength={max}
          aria-invalid={error ? true : undefined}
          onChange={(e) => {
            if (error && onTyped) onTyped();
            onChange(e.target.value);
          }}
        />
      </label>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      <Counter value={value} max={max} />
      {review && review.length > 0 && (
        <p className="hint">
          <span className="badge YELLOW">проверить после ИИ</span> {review.join("; ")}
        </p>
      )}
    </div>
  );
}

/**
 * §5.3–5.4 (FR-43): чипы источников у диагноза/лечения/рекомендаций.
 *
 * Чип раскрывается в цитату чанка КБ и ссылку на первоисточник: врач видит, на
 * чём основано предложение модели, прежде чем подписать протокол.
 */
function SourceChips({
  sources,
  ids,
  usedBy,
  review,
}: {
  sources: ProtocolSource[];
  /** id чанков, на которые ссылается элемент (diagnosis.sources / treatment[i].sources). */
  ids?: string[];
  /** Либо раздел целиком — по `used_by`. */
  usedBy?: string;
  review?: string[];
}) {
  const shown = ids && ids.length > 0
    ? sources.filter((source) => ids.includes(source.id))
    : usedBy
      ? sources.filter((source) => (source.used_by ?? []).includes(usedBy))
      : [];
  if (shown.length === 0 && (!review || review.length === 0)) return null;
  return (
    <div className="source-block">
      {shown.length > 0 && (
        <div className="chips">
          {shown.map((source) => (
            <details className="source-chip" key={source.id}>
              <summary>
                [{source.ref}] {source.title || source.section || "источник"}
                {source.section && source.title ? ` · ${source.section}` : ""}
              </summary>
              <p className="small muted">{source.quote}</p>
              {source.url && (
                <a className="small" href={source.url} target="_blank" rel="noreferrer">
                  Первоисточник
                </a>
              )}
            </details>
          ))}
        </div>
      )}
      <ReviewHint reasons={review} />
    </div>
  );
}

/** Причины `needs_review` раздела: бейдж + русский текст (коды модели не показываем). */
function ReviewHint({ reasons }: { reasons?: string[] }) {
  if (!reasons || reasons.length === 0) return null;
  return (
    <p className="hint">
      <span className="badge YELLOW">проверить</span> {reasons.join("; ")}
    </p>
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
  // Ошибки, привязанные к полям (Problem `fields`): текст рисуется у самого поля,
  // общий `err` остаётся только для сбоев без адреса (сеть, 500, чужая сессия).
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");

  /**
   * Показать сбой так, чтобы было видно, к какому полю он относится: подсказки —
   * у полей, первое проблемное поле — в зону видимости и с фокусом. Иначе после
   * «Подтвердить протокол» экран просто не менялся, а причина висела выше.
   */
  const showFailure = (e: unknown, what: string) => {
    const fields = e instanceof ApiError ? e.fields : {};
    const paths = Object.keys(fields);
    if (paths.length === 0) {
      setFieldErrors({});
      setErr(errText(e));
      return;
    }
    setFieldErrors(fields);
    setErr("");
    const detail = e instanceof ApiError ? e.detail.trim() : "";
    const where = `${paths.length} ${paths.length === 1 ? "поле" : "полей"}`;
    setNote(
      `${what}: ${detail || "проверьте выделенные поля"} (${where}) — подсказки у самих полей.`,
    );
    const first = paths[0];
    requestAnimationFrame(() => {
      const node = document.getElementById(fieldAnchorId(first));
      node?.scrollIntoView({ block: "center", behavior: "smooth" });
      node
        ?.querySelector<HTMLElement>("textarea, input, select")
        ?.focus({ preventScroll: true });
    });
  };

  const clearFieldErrors = () => {
    setFieldErrors((prev) => (Object.keys(prev).length ? {} : prev));
  };

  /** Врач правит поле — его подсказка больше не актуальна. */
  const clearFieldError = useCallback((path: string) => {
    setFieldErrors((prev) => {
      if (!prev[path]) return prev;
      const next = { ...prev };
      delete next[path];
      return next;
    });
  }, []);
  const [busy, setBusy] = useState(false);
  // Ключи строк лечения: `key={i}` после удаления из середины переиспользует DOM строки.
  const [rowKeys, setRowKeys] = useState<string[]>([]);
  const rowSeq = useRef(0);
  // Нумерация перенесённых источников: id в ответе модели сквозные только внутри
  // одного черновика, поэтому каждому черновику — свой префикс (§5.4).
  const draftSeq = useRef(0);
  // §4.2: свежая деталь сессии — источник ai_status, подписи и доставки.
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [review, setReview] = useState<Record<string, string[]>>({});
  const [conflicts, setConflicts] = useState<DraftConflict[]>([]);
  // Живые снимки: ответы сервера (черновик ИИ, сохранение) приходят в замыкания момента
  // клика, а слияние и сохранение обязаны считаться с тем, что врач напечатал за это время.
  const liveSections = useRef(sections);
  liveSections.current = sections;
  const liveProtocol = useRef<SessionProtocol | null>(protocol);
  liveProtocol.current = protocol;

  const refresh = useCallback(async () => {
    const fresh = await api.getSession(session.id);
    setDetail(fresh);
    setSession(fresh);
    const brief = fresh.protocol;
    const current = liveProtocol.current;
    if (brief && current && brief.version === current.version) {
      // Статус активной версии меняют подпись и передача.
      const merged = { ...current, status: brief.status };
      liveProtocol.current = merged;
      setProtocol(merged);
    } else if (brief && (!current || brief.version !== current.version)) {
      // Активную версию сменили в другой вкладке: показывать старую нельзя — подпись,
      // PDF и передача уходят на сервер и работают с версией, которой на экране нет.
      const reloaded = await api.getProtocol(session.id);
      liveProtocol.current = reloaded;
      setProtocol(reloaded);
      const serverSections = withDefaults(base, reloaded.sections);
      if (JSON.stringify(liveSections.current) === JSON.stringify(serverSections)) {
        liveSections.current = serverSections;
        setSections(serverSections);
        setConflicts([]);
        setReview({});
      } else {
        // Несохранённый текст врача не выбрасываем: сохранение создаст новую версию.
        setNote(
          `Протокол обновлён в другой вкладке (версия ${reloaded.version}) — ваши правки остались в редакторе.`,
        );
      }
    }
    return fresh;
  }, [session.id, base]);

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
    clearFieldErrors();
    setNote("");
    try {
      // Отправляем ровно то, что видит врач на момент клика.
      const sent = liveSections.current;
      const p = await api.putProtocol(session.id, sent, sent.diagnosis.icd10.trim() || null);
      liveProtocol.current = p;
      setProtocol(p);
      // Допечатанное за время round-trip не выбрасываем: серверное эхо переливаем в
      // редактор, только если он не изменился (сравнение по ссылке состояния).
      const typedLater = liveSections.current !== sent;
      if (!typedLater) {
        const next = withDefaults(base, p.sections);
        liveSections.current = next;
        setSections(next);
      }
      setConflicts([]);
      await refresh();
      setNote(
        typedLater
          ? `Черновик сохранён (версия ${p.version}). Текст, введённый во время сохранения, в версию не вошёл — сохраните ещё раз.`
          : `Черновик сохранён (версия ${p.version}).`,
      );
    } catch (e: unknown) {
      showFailure(e, "Сохранение не прошло");
    } finally {
      setBusy(false);
    }
  };

  /**
   * §10 (FR-44): «Применить» для предложения агента — обычный PUT, но только
   * одного раздела: агент протокол не меняет, это явное действие врача.
   * Возвращает текст ошибки для ленты чата (null — успех).
   */
  const applyProposal = async (proposal: SessionChatProposal): Promise<string | null> => {
    const next = withField(liveSections.current, proposal.section, proposal.after);
    try {
      const p = await api.putProtocol(
        session.id,
        next,
        next.diagnosis.icd10.trim() || null,
      );
      liveProtocol.current = p;
      const echo = withDefaults(base, p.sections);
      liveSections.current = echo;
      setProtocol(p);
      setSections(echo);
      setConflicts([]);
      await refresh();
      setNote(`Правка применена (версия ${p.version}).`);
      return null;
    } catch (e: unknown) {
      return errText(e);
    }
  };

  const approve = async () => {
    setBusy(true);
    setErr("");
    clearFieldErrors();
    setNote("");
    try {
      // Подтверждаем ровно то, что видит врач: approve уходит по последнему черновику,
      // поэтому несохранённые правки сначала сохраняем (§4.4 PUT → approve).
      let sent = liveSections.current;
      let saved = await api.putProtocol(session.id, sent, sent.diagnosis.icd10.trim() || null);
      liveProtocol.current = saved;
      setProtocol(saved);
      if (liveSections.current !== sent) {
        // Врач допечатал, пока шёл PUT: подтверждаем свежее состояние, а не снимок клика.
        sent = liveSections.current;
        saved = await api.putProtocol(session.id, sent, sent.diagnosis.icd10.trim() || null);
        liveProtocol.current = saved;
        setProtocol(saved);
      }
      const p = await api.approveProtocol(session.id);
      // Подтверждённая версия уходит из-под редактора: ввод в момент ответа уже не попадёт
      // в неё, поэтому говорим об этом прямо, а не молча его теряем.
      const typedDuringApprove = liveSections.current !== sent;
      const next = withDefaults(base, p.sections);
      liveProtocol.current = p;
      liveSections.current = next;
      setProtocol(p);
      setSections(next);
      setConflicts([]);
      setReview({});
      await refresh();
      setNote(
        typedDuringApprove
          ? "Протокол подтверждён. Текст, введённый в момент подтверждения, в версию не вошёл."
          : "Протокол подтверждён.",
      );
    } catch (e: unknown) {
      // 409 PROTOCOL_NOT_READY — список незаполненных разделов в detail.
      showFailure(e, "Подтверждение не прошло");
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
    // `onDraft` приходит из замыкания момента клика, а ответ модели — секунды спустя:
    // слияние считаем по живому тексту врача, иначе начатый им раздел выглядит пустым.
    const current = liveSections.current;
    let merged = current;
    const disputed: DraftConflict[] = [];
    const applied: string[] = [];
    for (const key of draftKeys(draft.sections)) {
      const proposed = fieldValue(draft.sections, key);
      if (!proposed.trim()) continue;
      const mine = fieldValue(current, key);
      if (!mine.trim()) {
        merged = withField(merged, key, proposed);
        applied.push(key);
      } else if (mine !== proposed) disputed.push({ key, next: proposed });
    }
    // FR-43: лечение и его обоснования переносим только целиком и только если
    // врач ещё не вёл лечение сам — иначе его назначения важнее черновика.
    const proposedRows = draft.sections.treatment ?? [];
    const treatmentKept = proposedRows.length > 0 && merged.treatment.length > 0;
    const rowsApplied = proposedRows.length > 0 && merged.treatment.length === 0;
    // §5.4: id источников нумеруются внутри одного ответа модели (S1…Sn), и новый
    // «S1» — это другой чанк. Источники, взятые из черновика, переносим в
    // собственное пространство имён и переписываем ссылки диагноза и лечения:
    // иначе подписанный документ сошлётся на чужое обоснование, а PUT отклонит
    // такую ссылку как PROTOCOL_SOURCE_UNKNOWN.
    const proposedSources = draft.sections.sources ?? [];
    const diagnosisApplied = applied.some((key) => key.startsWith("diagnosis."));
    const draftDiagnosisSources = draft.sections.diagnosis?.sources ?? [];
    let relabel = (ids: string[] | undefined) => ids ?? [];
    if ((rowsApplied || diagnosisApplied) && proposedSources.length > 0) {
      const prefix = `d${++draftSeq.current}-`;
      const mapping = new Map(proposedSources.map((s) => [s.id, s.id ? prefix + s.id : s.id]));
      relabel = (ids) => (ids ?? []).map((id) => mapping.get(id) ?? id);
      const known = new Set((merged.sources ?? []).map((s) => s.id));
      merged = {
        ...merged,
        sources: [
          ...(merged.sources ?? []),
          ...proposedSources
            .map((s) => ({ ...s, id: mapping.get(s.id) ?? s.id }))
            .filter((s) => !known.has(s.id)),
        ],
      };
      if (draftDiagnosisSources.length > 0) {
        merged = {
          ...merged,
          diagnosis: { ...merged.diagnosis, sources: relabel(draftDiagnosisSources) },
        };
      }
    }
    if (rowsApplied) {
      merged = {
        ...merged,
        treatment: proposedRows.map((row) => ({ ...row, sources: relabel(row.sources) })),
      };
    }
    liveSections.current = merged;
    liveProtocol.current = draft;
    setSections(merged);
    setConflicts(disputed);
    const nextReview = reviewByField(draft.needs_review);
    if (treatmentKept) {
      nextReview.treatment = [
        ...(nextReview.treatment ?? []),
        "модель предложила свои позиции — ваши назначения не перезаписаны",
      ];
    }
    setReview(nextReview);
    setProtocol(draft);
    setErr("");
    const grounded = draft.sections.sources?.length ?? 0;
    const hints = [
      grounded > 0 ? `обоснований из базы знаний: ${grounded}` : "обоснований нет",
    ];
    if (disputed.length > 0) {
      hints.push(`${disputed.length} раздел(ов) не перезаписаны — выберите ниже`);
    }
    setNote(
      `Черновик ИИ перенесён в редактор (${hints.join("; ")}). ` +
        "Проверьте разделы с бейджем и сохраните.",
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
      treatment: s.treatment.map((row, idx) =>
        // Правка врача фиксируется в подписываемом документе: видно, что позицию
        // менял человек, а не модель.
        idx === i ? { ...row, [key]: value, edited: true } : row,
      ),
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
        onApplyProposal={applyProposal}
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
              <dt>Обоснование диагноза (источники)</dt>
              <dd>
                <SourceChips sources={sections.sources ?? []} ids={sections.diagnosis.sources} />
              </dd>
              <dt>Лечение</dt>
              <dd>
                {sections.treatment.length === 0
                  ? "—"
                  : sections.treatment.map((row, i) => (
                      <div key={i}>
                        <div>
                          {row.drug} {row.dose} {row.schedule} {row.duration}
                          {row.edited ? " · изменено врачом" : ""}
                        </div>
                        <SourceChips sources={sections.sources ?? []} ids={row.sources} />
                      </div>
                    ))}
              </dd>
              <dt>Рекомендации</dt>
              <dd>{sections.recommendations || "—"}</dd>
              <dt>Источники (база знаний)</dt>
              <dd>
                <SourceChips
                  sources={sections.sources ?? []}
                  ids={(sections.sources ?? []).map((source) => source.id)}
                />
              </dd>
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
              path="passport.legal_basis"
              error={fieldErrors["passport.legal_basis"]}
              onTyped={() => clearFieldError("passport.legal_basis")}
              onChange={(v) => setSections((s) => ({ ...s, passport: { ...s.passport, legal_basis: v } }))}
            />

            <h3>Жалобы</h3>
            <NarrativeField
              label="Жалобы"
              value={sections.complaints}
              max={LIMIT.complaints}
              review={review["complaints"]}
              path="complaints"
              error={fieldErrors["complaints"]}
              onTyped={() => clearFieldError("complaints")}
              onChange={(v) => setSections((s) => ({ ...s, complaints: v }))}
            />

            <h3>Анамнез заболевания</h3>
            <NarrativeField
              label="Anamnesis morbi"
              value={sections.anamnesis_morbi}
              max={LIMIT.anamnesis_morbi}
              rows={4}
              review={review["anamnesis_morbi"]}
              path="anamnesis_morbi"
              error={fieldErrors["anamnesis_morbi"]}
              onTyped={() => clearFieldError("anamnesis_morbi")}
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
                path={`mental_status.${String(f.key)}`}
                error={fieldErrors[`mental_status.${String(f.key)}`]}
                onTyped={() => clearFieldError(`mental_status.${String(f.key)}`)}
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
              path="somatic_status"
              error={fieldErrors["somatic_status"]}
              onTyped={() => clearFieldError("somatic_status")}
              onChange={(v) => setSections((s) => ({ ...s, somatic_status: v }))}
            />

            <h3>Диагноз</h3>
            <div className="row">
              <label id="pf-diagnosis-syndrome">
                Синдром
                <input
                  value={sections.diagnosis.syndrome}
                  maxLength={LIMIT.line}
                  aria-invalid={fieldErrors["diagnosis.syndrome"] ? true : undefined}
                  onChange={(e) => {
                    clearFieldError("diagnosis.syndrome");
                    setSections((s) => ({
                      ...s,
                      diagnosis: { ...s.diagnosis, syndrome: e.target.value },
                    }));
                  }}
                />
                <FieldError text={fieldErrors["diagnosis.syndrome"]} />
              </label>
              <label id="pf-diagnosis-icd10">
                МКБ-10
                <input
                  value={sections.diagnosis.icd10}
                  maxLength={10}
                  placeholder="F41.1"
                  aria-invalid={fieldErrors["diagnosis.icd10"] ? true : undefined}
                  onChange={(e) => {
                    clearFieldError("diagnosis.icd10");
                    setSections((s) => ({
                      ...s,
                      diagnosis: { ...s.diagnosis, icd10: e.target.value.toUpperCase() },
                    }));
                  }}
                />
                <FieldError text={fieldErrors["diagnosis.icd10"]} />
              </label>
            </div>
            <NarrativeField
              label="Обоснование диагноза"
              value={sections.diagnosis.rationale}
              max={LIMIT.rationale}
              review={review["diagnosis.rationale"]}
              path="diagnosis.rationale"
              error={fieldErrors["diagnosis.rationale"]}
              onTyped={() => clearFieldError("diagnosis.rationale")}
              onChange={(v) => setSections((s) => ({ ...s, diagnosis: { ...s.diagnosis, rationale: v } }))}
            />
            <div id="pf-sources">
              <SourceChips
                sources={sections.sources ?? []}
                ids={sections.diagnosis.sources}
                review={[
                  ...(review["diagnosis"] ?? []),
                  ...(review["diagnosis.icd10"] ?? []),
                  ...(review["diagnosis.syndrome"] ?? []),
                ]}
              />
              <FieldError text={errorFor(fieldErrors, "sources")} />
            </div>

            <h3 id="pf-treatment">Лечение</h3>
            <ReviewHint reasons={review["treatment"]} />
            <FieldError text={fieldErrors["treatment"]} />
            {sections.treatment.length === 0 && <p className="small muted">Позиций нет.</p>}
            {sections.treatment.map((row, i) => (
              <div className="treatment-row" id={`pf-treatment-${i}`} key={rowKeys[i] ?? `t${i}`}>
                <div className="row">
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
                <FieldError text={errorFor(fieldErrors, `treatment[${i}]`)} />
                <div className="row">
                  {row.edited && <span className="badge YELLOW">изменено врачом</span>}
                  <SourceChips
                    sources={sections.sources ?? []}
                    ids={row.sources}
                    review={review[`treatment.${i}`]}
                  />
                </div>
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
              path="recommendations"
              error={fieldErrors["recommendations"]}
              onTyped={() => clearFieldError("recommendations")}
              onChange={(v) => setSections((s) => ({ ...s, recommendations: v }))}
            />
            <SourceChips sources={sections.sources ?? []} usedBy="recommendations" />

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
