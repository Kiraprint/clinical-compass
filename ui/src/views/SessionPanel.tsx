import { useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  AppointmentChannel,
  ClinicalSession,
  MentalStatus,
  ProtocolSections,
  ProtocolStatus,
  SessionProtocol,
  TreatmentRow,
} from "../api";

/**
 * FR-39: панель приёма и ручной протокол (docs/18 §4.2, §4.4, §5).
 *
 * Аудио/ИИ/подпись/PDF/ЕМИАС — PR P3…P5, здесь их нет: только ручной протокол и
 * «Завершить приём» (создаёт приём в карточке пациента). Потолки разделов §5.2
 * показаны теми же числами, что валидирует сервер (`maxLength` + счётчик у предела).
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
  onChange,
}: {
  label: string;
  value: string;
  max: number;
  rows?: number;
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
    </div>
  );
}

export default function SessionPanel({
  session: initial,
  onClose,
}: {
  session: ClinicalSession;
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

  useEffect(() => {
    let cancelled = false;
    api
      .getProtocol(session.id)
      .then((p) => {
        if (cancelled) return;
        setProtocol(p);
        setSections(withDefaults(base, p.sections));
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
  const minutes = session.ended_at
    ? Math.max(
        1,
        Math.round(
          (new Date(session.ended_at).getTime() - new Date(session.started_at).getTime()) / 60000,
        ),
      )
    : null;

  const save = async () => {
    setBusy(true);
    setErr("");
    setNote("");
    try {
      const code = sections.diagnosis.icd10.trim() || null;
      const p = await api.putProtocol(session.id, sections, code);
      setProtocol(p);
      setSections(withDefaults(base, p.sections));
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
      const p = await api.approveProtocol(session.id);
      setProtocol(p);
      setSections(withDefaults(base, p.sections));
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
      setNote("Приём завершён, запись добавлена в карточку пациента.");
    } catch (e: unknown) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const setTreatment = (i: number, key: keyof TreatmentRow, value: string) =>
    setSections((s) => ({
      ...s,
      treatment: s.treatment.map((row, idx) => (idx === i ? { ...row, [key]: value } : row)),
    }));

  return (
    <div>
      <div className="card">
        <div className="schedule-head">
          <h2>Приём: {session.patient_fio ?? "—"}</h2>
          <button className="ghost" onClick={onClose}>
            К расписанию
          </button>
        </div>
        <p className="small muted">
          {CHANNEL_LABELS[session.channel]} · начало {START_FMT.format(new Date(session.started_at))}{" "}
          · длительность {minutes === null ? "идёт" : `${minutes} мин`} · статус{" "}
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
              onChange={(v) => setSections((s) => ({ ...s, complaints: v }))}
            />

            <h3>Анамнез заболевания</h3>
            <NarrativeField
              label="Anamnesis morbi"
              value={sections.anamnesis_morbi}
              max={LIMIT.anamnesis_morbi}
              rows={4}
              onChange={(v) => setSections((s) => ({ ...s, anamnesis_morbi: v }))}
            />

            <h3>Психический статус</h3>
            {MENTAL_FIELDS.map((f) => (
              <NarrativeField
                key={f.key}
                label={f.label}
                value={sections.mental_status[f.key]}
                max={LIMIT.mental}
                rows={2}
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
              rows={2}
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
                      diagnosis: { ...s.diagnosis, icd10: e.target.value },
                    }))
                  }
                />
              </label>
            </div>
            <NarrativeField
              label="Обоснование диагноза"
              value={sections.diagnosis.rationale}
              max={LIMIT.rationale}
              rows={2}
              onChange={(v) => setSections((s) => ({ ...s, diagnosis: { ...s.diagnosis, rationale: v } }))}
            />

            <h3>Лечение</h3>
            {sections.treatment.length === 0 && <p className="small muted">Позиций нет.</p>}
            {sections.treatment.map((row, i) => (
              <div className="row" key={i}>
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
                  onClick={() =>
                    setSections((s) => ({
                      ...s,
                      treatment: s.treatment.filter((_, idx) => idx !== i),
                    }))
                  }
                >
                  Убрать
                </button>
              </div>
            ))}
            <button
              type="button"
              className="ghost"
              disabled={sections.treatment.length >= LIMIT.treatmentMax}
              onClick={() =>
                setSections((s) => ({
                  ...s,
                  treatment: [...s.treatment, { drug: "", dose: "", schedule: "", duration: "" }],
                }))
              }
            >
              Добавить позицию
            </button>

            <h3>Рекомендации</h3>
            <NarrativeField
              label="Рекомендации"
              value={sections.recommendations}
              max={LIMIT.recommendations}
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
    </div>
  );
}
