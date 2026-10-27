import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  ClinicalSession,
  SessionChatCitation,
  SessionChatMessage,
  SessionChatProposal,
  SessionDetail,
  SessionProtocol,
  SessionTranscript,
  SpeakerRole,
  TranscriptSegment,
} from "../api";

/**
 * FR-39 (docs/18 §4.3): ИИ-ассистент приёма — запись, расшифровка, черновик протокола.
 *
 * Красные линии, которые держит этот блок:
 *  - без отдельного согласия на аудиозапись (`audio_consent`) загрузка не уходит:
 *    сервер всё равно ответит 422, но и кнопку без согласия не показываем;
 *  - сырая речь врачу не показывается — сервер хранит только деперсонализированный
 *    текст, поэтому в расшифровке стоит явная пометка;
 *  - черновик никогда не перезаписывает введённый врачом текст: слияние делает
 *    панель (`onDraft`), здесь только запрос;
 *  - карточка не глушится, если ИИ выключен администратором: вместо мёртвых
 *    кнопок — информационный текст.
 */

/** §4.3 коды → человеческий текст: сам код в интерфейсе не показываем. */
const AI_ERRORS: Record<string, string> = {
  SESSION_AI_DISABLED: "ИИ-ассистент выключен администратором",
  MODEL_NOT_CONFIGURED: "Модель не настроена — обратитесь к администратору",
  DEPERSONALIZATION_UNAVAILABLE:
    "Деперсонализация недоступна: отправлять текст беседы модели запрещено",
  AI_BUSY: "Расшифровка этой сессии уже идёт — дождитесь результата",
  TRANSCRIPT_MISSING: "Расшифровки ещё нет: загрузите запись и запустите расшифровку",
  SESSION_FINISHED: "Приём завершён — запись и расшифровка недоступны",
  AUDIO_CONSENT_REQUIRED: "Нужно отдельное согласие пациента на аудиозапись",
  AUDIO_TOO_LARGE: "Файл записи слишком большой",
  AUDIO_UNSUPPORTED: "Формат записи не поддерживается",
  PROTOCOL_LOCKED: "Протокол подтверждён — ИИ его не перезаписывает (правка создаст новую версию)",
  DRAFT_FAILED: "Модель не вернула корректный черновик — попробуйте ещё раз",
  AUTODRAFT_FAILED: "Черновик не создался автоматически — нажмите «Сформировать черновик»",
  TRANSCRIBE_FAILED: "Расшифровка не удалась",
  TRANSCRIPT_TOO_LARGE: "Запись слишком длинная для расшифровки",
  ML_UNAVAILABLE: "Сервис расшифровки недоступен — попробуйте позже",
  ARTIFACT_CORRUPT: "Файл расшифровки повреждён",
  // FR-42: PATCH ролей — метки и роли проверяет сервер (422).
  SPEAKER_ROLE_DUPLICATE: "Врач и пациент должны быть разными участниками",
  SPEAKER_ROLE_INVALID: "Недопустимая роль участника",
  SPEAKER_UNKNOWN: "Такой метки нет в расшифровке — обновите страницу",
  // FR-44 (§10): диалог по сессии.
  MESSAGE_TOO_LONG: "Сообщение слишком длинное — сократите вопрос",
  MESSAGE_EMPTY: "Введите вопрос",
  CHAT_LIMIT_REACHED: "Лимит реплик по этому приёму исчерпан",
  SESSION_NOT_FOUND: "Сессия не найдена — обновите расписание",
  NO_ROSTER: "Пациент не закреплён за вами — данные недоступны",
  CONSENT_REQUIRED: "Нет активного согласия пациента — данные недоступны",
};

/** Ошибка ИИ по-русски: код не протекает в интерфейс (§6). */
export function aiErrorText(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return AI_ERRORS[e.code] ?? (e.detail || fallback);
  return e instanceof Error && e.message ? e.message : fallback;
}

const AI_STATUS_LABELS: Record<string, string> = {
  none: "запись ещё не загружена",
  recording: "идёт запись",
  transcribing: "расшифровка идёт",
  transcribed: "расшифровка готова",
  failed: "расшифровка не удалась",
};

const mmss = (seconds: number) => {
  const total = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
};

const fmtSize = (bytes: number) =>
  bytes >= 1024 * 1024
    ? `${(bytes / (1024 * 1024)).toFixed(1)} МБ`
    : `${Math.max(1, Math.round(bytes / 1024))} КБ`;

const POLL_MS = 2000;
const POLL_LIMIT_MS = 15 * 60 * 1000;

// Сколько ждём ответа браузера на запрос микрофона. Дольше — считаем, что запрос
// не показан (или закрыт без ответа), и возвращаем кнопку в рабочее состояние.
const MIC_REQUEST_TIMEOUT_MS = 20_000;

/** Кодек записи: webm есть не везде (Safari), поэтому первый поддерживаемый. */
const RECORDER_MIMES = ["audio/webm", "audio/mp4", "audio/ogg"];
function pickRecorderMime(): string | null {
  if (typeof MediaRecorder === "undefined") return null;
  for (const candidate of RECORDER_MIMES) {
    if (typeof MediaRecorder.isTypeSupported !== "function") return candidate;
    if (MediaRecorder.isTypeSupported(candidate)) return candidate;
  }
  return null;
}

const mimeExtension = (value: string) =>
  value.includes("mp4") ? "m4a" : value.includes("ogg") ? "ogg" : "webm";


const ROLE_LABELS: Record<"clinician" | "patient", string> = {
  clinician: "Врач",
  patient: "Пациент",
};

function SpeakerBadge({ segment }: { segment: TranscriptSegment }) {
  // `unknown` — не роль, а «врач не знает, кто это»: показываем метку кластера.
  const label =
    segment.role === "clinician" || segment.role === "patient"
      ? ROLE_LABELS[segment.role]
      : segment.speaker
        ? `Говорящий ${segment.speaker}`
        : segment.role === "unknown"
          ? "Говорящий"
          : null;
  if (!label) return null;
  // Врач и пациент различаются цветом: в длинной расшифровке читают по бейджу.
  const color =
    segment.role === "clinician" ? " GREEN" : segment.role === "patient" ? " YELLOW" : "";
  return <span className={`badge${color}`}>{label}</span>;
}

function TranscriptView({
  transcript,
  onRoles,
  busy,
}: {
  transcript: SessionTranscript;
  onRoles: (roles: Record<string, SpeakerRole>) => void;
  busy: boolean;
}) {
  const segments: TranscriptSegment[] = transcript.segments;
  const speakers = Array.from(
    new Set(segments.map((s) => s.speaker).filter((s): s is string => !!s)),
  );
  // API на шаг старше бандла (раскатка контейнеров) может не прислать поля
  // диаризации: без значений по умолчанию карточка падала бы на `roles[first]`.
  const roles = transcript.speaker_roles ?? {};
  const diarized = transcript.diarized ?? false;
  const assumed = transcript.speaker_roles_assumed ?? false;
  const [first, second] = speakers;
  const firstIsDoctor = roles[first] === "clinician" || !roles[first];
  return (
    <div>
      <p className="hint">
        Текст деперсонализирован: имена, адреса, телефоны и даты заменены маской — исходная
        запись беседы сервером не сохраняется.
        {transcript.duration_sec ? ` Длительность ${mmss(transcript.duration_sec)}.` : ""}
        {transcript.model ? ` Модель: ${transcript.model}.` : ""}
      </p>
      {!diarized && (
        <p className="small muted">
          Говорящие не разделены (модель диаризации недоступна) — реплики идут одним потоком.
        </p>
      )}
      {diarized && speakers.length === 2 && (
        <div className="row" style={{ marginTop: 8 }}>
          <span className="small muted">
            {assumed
              ? `Роли проставлены автоматически: «${first}» говорит первой — проверьте.`
              : "Роли участников подтверждены врачом."}
          </span>
          {assumed && (
            <button
              type="button"
              data-testid="speakers-confirm-role"
              disabled={busy}
              onClick={() => onRoles({ [first]: "clinician", [second]: "patient" })}
            >
              {`«${first}» — это врач`}
            </button>
          )}
          <button
            type="button"
            className="ghost"
            data-testid="speakers-swap"
            disabled={busy}
            onClick={() =>
              // «Переставить» — перестановка, а не догадка: пациент становится врачом.
              onRoles(
                firstIsDoctor
                  ? { [first]: "patient", [second]: "clinician" }
                  : { [first]: "clinician", [second]: "patient" },
              )
            }
          >
            Переставить врача и пациента
          </button>
        </div>
      )}
      {segments.length === 0 ? (
        <p className="small muted">В расшифровке нет реплик.</p>
      ) : (
        <ol className="transcript" aria-label="Расшифровка приёма">
          {segments.map((segment, i) => (
            <li key={i} className="transcript-segment">
              <span className="transcript-time">{mmss(segment.start)}</span>
              <SpeakerBadge segment={segment} />
              <span className="transcript-text">{segment.text}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** §10 (FR-44): подписи разделов для `proposal.section` — один словарь с панелью. */
const SECTION_LABELS: Record<string, string> = {
  complaints: "Жалобы",
  anamnesis_morbi: "Анамнез",
  somatic_status: "Соматический статус",
  recommendations: "Рекомендации",
  "diagnosis.rationale": "Обоснование диагноза",
  "mental_status.appearance_behavior": "Внешний вид и поведение",
  "mental_status.consciousness_orientation": "Сознание и ориентировка",
  "mental_status.perception": "Восприятие",
  "mental_status.thinking_speech": "Мышление и речь",
  "mental_status.emotions_volition": "Эмоции и воля",
  "mental_status.criticism": "Критика",
  "mental_status.suicidal_risk": "Суицидальный риск",
};

function ChatSources({ citations }: { citations: SessionChatCitation[] }) {
  if (citations.length === 0) return null;
  return (
    <div className="chips">
      {citations.map((citation, i) => (
        <span className="source-chip" key={`${citation.guideline_id}-${i}`} title={citation.topic}>
          [{i + 1}] {citation.source_title || citation.guideline_id}
        </span>
      ))}
    </div>
  );
}

/**
 * §10: «Спросить ИИ» — диалог по текущему приёму.
 *
 * Агент ничего не меняет сам: правка раздела приходит как `proposal`, а
 * применяет её врач кнопкой — панель делает обычный `PUT …/protocol`. Кнопка
 * неактивна, когда протокол уже подтверждён: правка подтверждённого документа —
 * отдельное решение врача, а не побочный эффект чата.
 */
function SessionChat({
  sessionId,
  locked,
  onApplyProposal,
}: {
  sessionId: string;
  locked: boolean;
  onApplyProposal: (proposal: SessionChatProposal) => Promise<string | null>;
}) {
  const [items, setItems] = useState<SessionChatMessage[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [applied, setApplied] = useState<string[]>([]);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    api
      .sessionChatHistory(sessionId)
      .then((data) => {
        if (alive.current) setItems(data.items);
      })
      .catch(() => {
        // История — вспомогательная лента: без неё чат работает.
      });
    return () => {
      alive.current = false;
    };
  }, [sessionId]);

  const send = async () => {
    const message = text.trim();
    if (!message || busy) return;
    setBusy(true);
    setErr("");
    setNote("");
    try {
      const reply = await api.sessionChat(sessionId, message);
      if (!alive.current) return;
      setItems((prev) => [
        ...prev,
        {
          id: `local-${prev.length}-${Date.now()}`,
          role: "clinician",
          content: message,
          citations: [],
          proposal: null,
          created_at: null,
        },
        reply,
      ]);
      setText("");
    } catch (e: unknown) {
      if (alive.current) setErr(aiErrorText(e, "Не удалось получить ответ"));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const apply = async (item: SessionChatMessage, proposal: SessionChatProposal) => {
    setErr("");
    const failure = await onApplyProposal(proposal);
    if (failure) {
      setErr(failure);
      return;
    }
    setNote(`Правка применена: ${SECTION_LABELS[proposal.section] ?? proposal.section}`);
    setApplied((prev) => [...prev, item.id]);
  };

  return (
    <div style={{ marginTop: 12 }}>
      <p className="small muted">
        Спросить ИИ по этому приёму: ответ собирается по базе знаний с цитатами. Раздел протокола
        сам не меняется — правку применяете вы.
      </p>
      <div className="row">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder="Например: что уточнить в разделе рекомендаций?"
          maxLength={2000}
          disabled={busy}
          aria-label="Вопрос ИИ по приёму"
        />
        <button type="button" onClick={() => void send()} disabled={busy || text.trim() === ""}>
          {busy ? "Думает…" : "Спросить"}
        </button>
      </div>
      {err && (
        <p className="error" role="alert">
          {err}
        </p>
      )}
      {note && !err && <p className="small muted">{note}</p>}
      {items.length > 0 && (
        <div className="chat-feed" aria-live="polite">
          {items.map((item) => (
            <div className={`chat-msg ${item.role}`} key={item.id}>
              <span className="small muted">{item.role === "clinician" ? "Вы" : "ИИ"}</span>
              <p>{item.content}</p>
              <ChatSources citations={item.citations} />
              {item.proposal && (
                <div className="proposal">
                  <p className="small">
                    Предложение:{" "}
                    {SECTION_LABELS[item.proposal.section] ?? item.proposal.section}
                  </p>
                  {item.proposal.before && (
                    <p className="small muted">Было: {item.proposal.before}</p>
                  )}
                  <p className="small">Станет: {item.proposal.after}</p>
                  {item.proposal.reason && (
                    <p className="small muted">{item.proposal.reason}</p>
                  )}
                  <div className="chips">
                    {item.proposal.sources.map((source, i) => (
                      <span className="source-chip" key={`${source.guideline_id}-${i}`}>
                        [{source.ref}] {source.source_title || source.guideline_id}
                      </span>
                    ))}
                  </div>
                  <button
                    type="button"
                    className="ghost"
                    disabled={locked || applied.includes(item.id)}
                    onClick={() => void apply(item, item.proposal as SessionChatProposal)}
                  >
                    {applied.includes(item.id)
                      ? "Применено"
                      : locked
                        ? "Протокол подтверждён"
                        : "Применить в раздел"}
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


export default function SessionAssistantCard({
  session,
  protocolLocked,
  hasAudio,
  onRefresh,
  onDraft,
  onApplyProposal,
}: {
  session: ClinicalSession;
  /** Протокол подтверждён/подписан — черновик ИИ запрещён (409 PROTOCOL_LOCKED). */
  protocolLocked: boolean;
  /** В сессии уже есть загруженная запись (`artifacts.kind='audio'`) — можно расшифровать. */
  hasAudio: boolean;
  /** Свежая деталь сессии: `ai_status`/`ai_error` живут только там. */
  onRefresh: () => Promise<SessionDetail | null>;
  /** Черновик отдан панели: слияние и бейджи проверки — её ответственность. */
  onDraft: (draft: SessionProtocol) => void;
  /** §10: применить `proposal` — панель делает обычный PUT и обновляет редактор. */
  onApplyProposal: (proposal: SessionChatProposal) => Promise<string | null>;
}) {
  // null — доступность ещё проверяется: карточка без ответа сервера не рисуется.
  const [available, setAvailable] = useState<boolean | null>(null);
  const [transcript, setTranscript] = useState<SessionTranscript | null>(null);
  const [aiStatus, setAiStatus] = useState(session.ai_status);
  const [aiError, setAiError] = useState<string | null>(null);
  const [audio, setAudio] = useState<{ blob: Blob; name: string } | null>(null);
  const [uploaded, setUploaded] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  // Явное состояние доступа к микрофону: idle → requesting → granted | denied.
  // При `denied` кнопка записи бесполезна, остаётся загрузка файла.
  const [mic, setMic] = useState<"idle" | "requesting" | "granted" | "denied">("idle");
  const [recSec, setRecSec] = useState(0);
  const [busy, setBusy] = useState<
    "" | "consent" | "upload" | "transcribe" | "draft" | "load" | "speakers"
  >("");
  const [polling, setPolling] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");

  const recorder = useRef<MediaRecorder | null>(null);
  // Кодек выбираем по возможностям браузера (Safari не умеет audio/webm).
  const mime = useRef("audio/webm");
  const chunks = useRef<Blob[]>([]);
  const stream = useRef<MediaStream | null>(null);
  const alive = useRef(true);
  // Опрос расшифровки — цепочкой setTimeout: снимается на размонтировании, без утечки.
  const pollTimer = useRef<number | null>(null);
  const pollDeadline = useRef(0);
  // Живость всей цепочки опроса: `pollTimer === null` бывает и внутри тика, пока запрос
  // в полёте, — по нему нельзя судить, идёт ли опрос.
  const chain = useRef(false);

  /** Микрофон отпускаем всегда: и на «стоп», и на размонтировании. */
  const releaseStream = useCallback(() => {
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
  }, []);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      if (recorder.current?.state === "recording") recorder.current.stop();
      recorder.current = null;
      releaseStream();
      chain.current = false;
      if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    };
  }, [releaseStream]);

  useEffect(() => {
    if (!recording) return;
    const tick = setInterval(() => setRecSec((s) => s + 1), 1000);
    return () => clearInterval(tick);
  }, [recording]);

  // Состояние разрешения, где браузер его отдаёт; иначе узнаем при первом запросе.
  useEffect(() => {
    const permissions = navigator.permissions;
    if (!permissions?.query) return;
    let cancelled = false;
    let status: PermissionStatus | null = null;
    const apply = (state: PermissionState) => {
      if (cancelled) return;
      if (state === "granted") setMic("granted");
      else if (state === "denied") setMic("denied");
      else setMic((cur) => (cur === "granted" ? "granted" : "idle"));
    };
    permissions
      .query({ name: "microphone" as PermissionName })
      .then((s) => {
        if (cancelled) return;
        status = s;
        apply(s.state);
        s.onchange = () => apply(s.state);
      })
      .catch(() => {
        // Firefox/Safari не знают `microphone` — состояние выяснится при записи.
      });
    return () => {
      cancelled = true;
      if (status) status.onchange = null;
    };
  }, []);

  const loadTranscript = useCallback(async () => {
    try {
      const t = await api.getTranscript(session.id);
      if (!alive.current) return;
      setTranscript(t);
    } catch (e: unknown) {
      if (!alive.current) return;
      // 404 = расшифровки ещё нет: это не ошибка, а обычное состояние до запуска.
      if (e instanceof ApiError && e.status === 404) return;
      setErr(aiErrorText(e, "Не удалось загрузить расшифровку"));
    }
  }, [session.id]);

  /** FR-42: роли врач/пациент подтверждает человек — сервер снимает `assumed`. */
  const saveRoles = async (roles: Record<string, SpeakerRole>) => {
    setBusy("speakers");
    setErr("");
    setNote("");
    try {
      const updated = await api.setSpeakerRoles(session.id, roles);
      if (!alive.current) return;
      setTranscript(updated);
      toast("Роли участников сохранены.");
    } catch (e: unknown) {
      setErr(aiErrorText(e, "Не удалось сохранить роли участников"));
    } finally {
      setBusy("");
    }
  };

  // Доступность ИИ определяется самим сервером: 503 SESSION_AI_DISABLED — карточка
  // становится информационной, 404 TRANSCRIPT_MISSING — ИИ работает, расшифровки нет.
  // Зависимость только от id: обновление `ai_status` не должно сбрасывать запись и файл.
  useEffect(() => {
    let cancelled = false;
    setAvailable(null);
    setTranscript(null);
    setUploaded(null);
    setAudio(null);
    api
      .getTranscript(session.id)
      .then((t) => {
        if (cancelled) return;
        setAvailable(true);
        setTranscript(t);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 404) {
          setAvailable(true);
          return;
        }
        if (e instanceof ApiError && e.code === "SESSION_AI_DISABLED") {
          setAvailable(false);
          return;
        }
        // Прочие сбои не прячут карточку целиком: врач видит причину и может повторить.
        setAvailable(true);
        setErr(aiErrorText(e, "Расшифровка недоступна"));
      });
    return () => {
      cancelled = true;
    };
  }, [session.id]);

  // Сервер — источник `ai_status`; пока идёт опрос, статус двигает сам опрос.
  useEffect(() => {
    if (!polling) setAiStatus(session.ai_status);
  }, [session.ai_status, polling]);

  // Расшифровку могли запустить до перезагрузки панели: продолжаем опрос, а не висим
  // с надписью «идёт» без единого запроса.
  useEffect(() => {
    if (session.ai_status === "transcribing" && !chain.current) startPoll();
    // eslint-disable-next-line react-hooks/exhaustive-deps — startPoll стабилен по refs
  }, [session.ai_status]);

  const toast = (ok: string) => {
    setNote(ok);
    setErr("");
  };

  const toggleConsent = async (next: boolean) => {
    setBusy("consent");
    setErr("");
    setNote("");
    try {
      await api.patchAudioConsent(session.id, next);
      await onRefresh();
      toast(next ? "Согласие на аудиозапись зафиксировано." : "Согласие на аудиозапись снято.");
    } catch (e: unknown) {
      setErr(aiErrorText(e, "Не удалось сохранить согласие"));
    } finally {
      setBusy("");
    }
  };

  const startRecording = async () => {
    setErr("");
    setNote("");
    const codec = pickRecorderMime();
    if (!codec) {
      setErr("Браузер не умеет записывать звук — загрузите файл записи.");
      return;
    }
    let granted = false;
    // Флаги таймаута: победил он или сам запрос — от этого зависит, надо ли гасить
    // пришедший позже поток. Проверять `stream.current` нельзя: реакция на `pending`
    // срабатывает раньше присваивания, и победивший поток гасился бы у живого рекордера.
    let timedOut = false;
    let micTimer = 0;
    setMic("requesting");
    try {
      // Если браузер так и не показал запрос (или его закрыли без ответа), промис
      // не завершается — состояние «Запрашиваем доступ» зависло бы навсегда.
      const pending = navigator.mediaDevices.getUserMedia({ audio: true });
      const timeout = new Promise<never>((_, reject) => {
        micTimer = window.setTimeout(() => {
          timedOut = true;
          reject(new DOMException("Запрос доступа к микрофону не завершился", "TimeoutError"));
        }, MIC_REQUEST_TIMEOUT_MS);
      });
      pending.then(
        (late) => {
          // Разрешили уже после таймаута: поток не нужен, но микрофон нельзя держать занятым.
          if (timedOut) late.getTracks().forEach((track) => track.stop());
        },
        () => undefined,
      );
      const media = await Promise.race([pending, timeout]);
      window.clearTimeout(micTimer);
      granted = true;
      setMic("granted");
      stream.current = media;
      chunks.current = [];
      mime.current = codec;
      const recorderInstance = new MediaRecorder(media, { mimeType: codec });
      recorderInstance.ondataavailable = (event) => {
        if (event.data.size > 0) chunks.current.push(event.data);
      };
      recorderInstance.onstop = () => {
        releaseStream();
        if (!alive.current) return;
        const blob = new Blob(chunks.current, { type: mime.current });
        setAudio({
          blob,
          name: `приём-${session.id.slice(0, 8)}.${mimeExtension(mime.current)}`,
        });
        setUploaded(null);
      };
      recorderInstance.start();
      recorder.current = recorderInstance;
      setRecSec(0);
      setRecording(true);
    } catch (e: unknown) {
      // Таймер таймаута не должен переживать неудачную попытку: его отклонение
      // прилетело бы в уже завершённый `race` и держало бы замыкание живым.
      window.clearTimeout(micTimer);
      // Микрофон могли отдать, а рекордер (кодек) — нет: поток всё равно отпускаем,
      // иначе индикатор записи горит, а следующий `getUserMedia` теряет первый поток.
      releaseStream();
      recorder.current = null;
      setRecording(false);
      if (granted) {
        setErr("Браузер не смог начать запись — загрузите файл записи.");
        return;
      }
      // `denied` — только реальный запрет. Отсутствие устройства или занятый
      // микрофон не должны показывать «разрешите доступ»: инструкция была бы ложной.
      const name = e instanceof DOMException ? e.name : "";
      if (name === "NotAllowedError" || name === "PermissionDeniedError" || name === "SecurityError") {
        setMic("denied");
        setErr(
          "Микрофон запрещён. Разрешите доступ к нему в настройках сайта " +
            "(значок замка в адресной строке) или загрузите файл записи.",
        );
        return;
      }
      setMic("idle");
      setErr(
        name === "TimeoutError"
          ? "Браузер не показал запрос доступа к микрофону. Проверьте разрешения сайта или загрузите файл записи."
          : name === "NotFoundError" || name === "DevicesNotFoundError"
            ? "Микрофон не найден — подключите его или загрузите файл записи."
            : name === "NotReadableError" || name === "TrackStartError"
              ? "Микрофон занят другим приложением — освободите его или загрузите файл записи."
              : "Не удалось включить микрофон — загрузите файл записи.",
      );
    }
  };

  const stopRecording = () => {
    setRecording(false);
    recorder.current?.stop();
    recorder.current = null;
  };

  const upload = async () => {
    if (!audio) return;
    setBusy("upload");
    setErr("");
    setNote("");
    try {
      const result = await api.uploadAudio(session.id, audio.blob, audio.name);
      if (!alive.current) return;
      setUploaded(result.artifact_id);
      setAudio(null);
      // Свежая запись обесценивает прошлую расшифровку (даже если сервер ещё не ответил).
      setTranscript(null);
      const detail = await onRefresh().catch(() => null);
      if (detail) setAiStatus(detail.ai_status);
      toast(`Запись загружена (${fmtSize(result.size_bytes)}).`);
    } catch (e: unknown) {
      setErr(aiErrorText(e, "Не удалось загрузить запись"));
    } finally {
      setBusy("");
    }
  };

  /** Один тик опроса `GET /clinician/sessions/{id}` (§4.3): терминальный статус — стоп. */
  const pollTick = async () => {
    pollTimer.current = null;
    if (!alive.current) return;
    if (Date.now() > pollDeadline.current) {
      chain.current = false;
      setPolling(false);
      setErr("Расшифровка идёт дольше 15 минут — нажмите «Обновить состояние».");
      return;
    }
    let detail: SessionDetail | null = null;
    try {
      detail = await onRefresh();
    } catch {
      detail = null; // сеть моргнула — следующий тик попробует снова
    }
    if (!alive.current) return;
    if (detail) {
      setAiStatus(detail.ai_status);
      if (detail.ai_status === "transcribed") {
        chain.current = false;
        setPolling(false);
        // Расшифровка могла пройти, а автодрафт — нет: код с сервера не теряем.
        setAiError(detail.ai_error ?? null);
        await loadTranscript();
        toast("Расшифровка готова.");
        return;
      }
      if (detail.ai_status === "failed") {
        chain.current = false;
        setPolling(false);
        setAiError(detail.ai_error ?? "TRANSCRIBE_FAILED");
        setErr(
          `Расшифровка не удалась: ${AI_ERRORS[detail.ai_error ?? ""] ?? "сервер сообщил об ошибке"}`,
        );
        return;
      }
    }
    pollTimer.current = window.setTimeout(() => void pollTick(), POLL_MS);
  };

  const startPoll = () => {
    pollDeadline.current = Date.now() + POLL_LIMIT_MS;
    chain.current = true;
    setPolling(true);
    if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
    pollTimer.current = window.setTimeout(() => void pollTick(), POLL_MS);
  };

  const transcribe = async () => {
    setBusy("transcribe");
    setErr("");
    setNote("");
    try {
      await api.transcribe(session.id);
      const detail = await onRefresh().catch(() => null);
      setAiStatus(detail?.ai_status ?? "transcribing");
      startPoll();
    } catch (e: unknown) {
      // Запуск не состоялся (409 AI_BUSY/SESSION_FINISHED, 503 …): статус — от сервера,
      // иначе кнопка навсегда останется «Расшифровка идёт…».
      setErr(aiErrorText(e, "Не удалось запустить расшифровку"));
      const detail = await onRefresh().catch(() => null);
      if (detail) {
        setAiStatus(detail.ai_status);
        setAiError(detail.ai_error ?? null);
      }
    } finally {
      setBusy("");
    }
  };

  /** Ручное обновление после сбоя опроса — без бесконечного спиннера. */
  const refreshState = async () => {
    setBusy("load");
    setErr("");
    setNote("");
    try {
      const detail = await onRefresh();
      if (!detail) return;
      setAiStatus(detail.ai_status);
      if (detail.ai_status === "transcribed") {
        setAiError(detail.ai_error ?? null);
        await loadTranscript();
        toast("Расшифровка получена.");
      } else if (detail.ai_status === "failed") {
        setAiError(detail.ai_error ?? "TRANSCRIBE_FAILED");
        setErr(`Расшифровка не удалась: ${AI_ERRORS[detail.ai_error ?? ""] ?? "сервер сообщил об ошибке"}`);
      } else if (detail.ai_status === "transcribing") {
        // После дедлайна опрос остановлен: ручное обновление снова его включает.
        setAiError(detail.ai_error ?? null);
        toast("Расшифровка идёт — опрос продолжен.");
        startPoll();
      } else {
        toast(`Состояние ИИ: ${AI_STATUS_LABELS[detail.ai_status] ?? detail.ai_status}.`);
      }
    } catch (e: unknown) {
      setErr(aiErrorText(e, "Не удалось обновить состояние"));
    } finally {
      setBusy("");
    }
  };

  const makeDraft = async () => {
    setBusy("draft");
    setErr("");
    setNote("");
    try {
      const draft = await api.draftProtocol(session.id);
      if (!alive.current) return;
      // FR-43: врач сразу видит, чем модель обосновала черновик, — чипы и бейджи
      // уже в редакторе панели, здесь короткая сводка к кнопке.
      const grounded = draft.sections?.sources?.length ?? 0;
      const flags = draft.needs_review?.length ?? 0;
      setNote(
        grounded > 0
          ? `Черновик готов: обоснований из базы знаний — ${grounded}, отметок «проверить» — ${flags}.`
          : "Черновик готов без обоснований: база знаний не дала источников — лечение проверьте вручную.",
      );
      setAiError(null); // черновик есть — подсказка о сбое автодрафта больше не нужна
      onDraft(draft);
    } catch (e: unknown) {
      setErr(aiErrorText(e, "Не удалось сформировать черновик"));
    } finally {
      setBusy("");
    }
  };

  // Приём завершили посреди записи: рекордер и микрофон останавливаем, а не тянем дальше.
  const sessionFinished = session.status === "finished";
  useEffect(() => {
    if (!sessionFinished) return;
    setRecording(false);
    recorder.current?.stop();
    recorder.current = null;
  }, [sessionFinished]);

  if (available === null) return null;

  if (!available) {
    return (
      <details className="card" open>
        <summary className="collapse-summary">ИИ-ассистент</summary>
        <p className="small muted">
          ИИ-ассистент выключен администратором: запись, расшифровка и черновик протокола
          недоступны. Протокол заполняется вручную — на подпись и передачу это не влияет.
        </p>
      </details>
    );
  }

  const consent = session.audio_consent;
  const finished = sessionFinished;
  const audioReady = !!uploaded || hasAudio;
  const canTranscribe = audioReady && !polling && !finished && aiStatus !== "transcribing";
  const transcribeLabel =
    aiStatus === "transcribing"
      ? "Расшифровка идёт…"
      : aiStatus === "failed"
        ? "Повторить расшифровку"
        : "Расшифровать";

  return (
    <details className="card">
      <summary className="collapse-summary">ИИ-ассистент</summary>
      <p className="hint">
        Запись беседы — отдельное согласие пациента (спецкатегория ПДн). Расшифровка
        деперсонализируется до сохранения: исходная речь в базе не остаётся. ИИ не назначает
        лечение — дозировки и суицидальный риск проверяет врач.
      </p>

      <label className="row">
        <input
          type="checkbox"
          checked={consent}
          disabled={busy !== "" || finished}
          onChange={(e) => void toggleConsent(e.target.checked)}
          aria-label="Согласие пациента на аудиозапись"
        />
        <span>Согласие пациента на аудиозапись приёма</span>
      </label>

      {finished && <p className="small muted">Приём завершён — запись недоступна.</p>}

      {!finished && (
        <div className="mic-state" aria-live="polite">
          {recording ? (
            <span className="small">Идёт запись ({mmss(recSec)})</span>
          ) : mic === "denied" ? (
            <span className="small muted">
              Микрофон запрещён — разрешите доступ в настройках сайта или загрузите файл записи.
            </span>
          ) : mic === "requesting" ? (
            <span className="small muted">Запрашиваем доступ к микрофону…</span>
          ) : mic === "granted" ? (
            <span className="small muted">Микрофон разрешён — можно начинать запись.</span>
          ) : (
            <span className="small muted">Чтобы записать приём, разрешите микрофон.</span>
          )}
        </div>
      )}

      {!finished && (
        <div className="row" style={{ marginTop: 8 }}>
          {recording ? (
            <button type="button" className="danger" onClick={stopRecording}>
              Остановить запись ({mmss(recSec)})
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void startRecording()}
              disabled={!consent || mic === "denied" || mic === "requesting"}
            >
              {mic === "granted" ? "Начать запись" : "Разрешить микрофон"}
            </button>
          )}
          <label className="row" style={{ margin: 0 }}>
            <span className="small muted">или файл</span>
            <input
              type="file"
              accept="audio/*"
              aria-label="Файл записи приёма"
              disabled={!consent || recording}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                setAudio({ blob: file, name: file.name });
                setUploaded(null);
                setNote("");
                setErr("");
              }}
            />
          </label>
        </div>
      )}

      {!consent && !finished && (
        <p className="small muted">
          Без согласия на аудиозапись запись и загрузка заблокированы: это отдельное основание
          обработки, а не часть общего согласия на приём.
        </p>
      )}

      {audio && (
        <div className="row" style={{ marginTop: 8 }}>
          <span className="small muted">
            {audio.name} · {fmtSize(audio.blob.size)}
          </span>
          <button type="button" onClick={() => void upload()} disabled={busy !== "" || !consent}>
            {busy === "upload" ? "Загрузка…" : "Загрузить запись"}
          </button>
        </div>
      )}

      <div className="row" style={{ marginTop: 8 }}>
        <span className="small muted">Статус: {AI_STATUS_LABELS[aiStatus] ?? aiStatus}</span>
        <button
          type="button"
          onClick={() => void transcribe()}
          disabled={!canTranscribe || busy !== ""}
        >
          {busy === "transcribe" ? "Запуск…" : transcribeLabel}
        </button>
        {polling && <span className="small muted">опрос каждые 2 с…</span>}
        {aiStatus === "transcribed" && !transcript && (
          <button type="button" className="ghost" onClick={() => void refreshState()} disabled={busy !== ""}>
            Показать расшифровку
          </button>
        )}
        {(aiError || err || (aiStatus === "transcribing" && !polling)) && (
          <button type="button" className="ghost" onClick={() => void refreshState()} disabled={busy !== ""}>
            Обновить состояние
          </button>
        )}
      </div>

      {audioReady && (
        <p className="small muted">
          Запись загружена{aiStatus === "transcribed" ? ", расшифровка готова" : "; запустите расшифровку"}.
        </p>
      )}

      {aiError && aiStatus === "transcribed" && !err && (
        <p className="small error">
          {AI_ERRORS[aiError] ?? "ИИ-шаг после расшифровки не выполнился — нажмите «Сформировать черновик»"}
        </p>
      )}

      {transcript && (
        <TranscriptView
          transcript={transcript}
          onRoles={(roles) => void saveRoles(roles)}
          busy={busy !== ""}
        />
      )}

      <div className="row" style={{ marginTop: 10 }}>
        <button
          type="button"
          onClick={() => void makeDraft()}
          disabled={!transcript || protocolLocked || busy !== ""}
        >
          {busy === "draft" ? "Формирование…" : "Сформировать черновик протокола"}
        </button>
        {protocolLocked && (
          <span className="small muted">Протокол подтверждён — черновик ИИ недоступен.</span>
        )}
        {!transcript && !protocolLocked && (
          <span className="small muted">Черновик строится по готовой расшифровке.</span>
        )}
        {/* FR-44 §10: конвейер делает черновик сам сразу после расшифровки. */}
        {transcript && !protocolLocked && (
          <span className="small muted">
            Черновик создаётся автоматически после расшифровки — кнопка нужна, только если он не
            появился.
          </span>
        )}
      </div>

      <SessionChat
        sessionId={session.id}
        locked={protocolLocked}
        onApplyProposal={onApplyProposal}
      />

      {err && (
        <p className="error" role="alert">
          {err}
        </p>
      )}
      {note && !err && <p className="small muted">{note}</p>}
    </details>
  );
}
