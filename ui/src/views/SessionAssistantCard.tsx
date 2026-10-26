import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  ClinicalSession,
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
  TRANSCRIBE_FAILED: "Расшифровка не удалась",
  TRANSCRIPT_TOO_LARGE: "Запись слишком длинная для расшифровки",
  ML_UNAVAILABLE: "Сервис расшифровки недоступен — попробуйте позже",
  ARTIFACT_CORRUPT: "Файл расшифровки повреждён",
  // FR-42: PATCH ролей — метки и роли проверяет сервер (422).
  SPEAKER_ROLE_DUPLICATE: "Врач и пациент должны быть разными участниками",
  SPEAKER_ROLE_INVALID: "Недопустимая роль участника",
  SPEAKER_UNKNOWN: "Такой метки нет в расшифровке — обновите страницу",
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

export default function SessionAssistantCard({
  session,
  protocolLocked,
  hasAudio,
  onRefresh,
  onDraft,
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
}) {
  // null — доступность ещё проверяется: карточка без ответа сервера не рисуется.
  const [available, setAvailable] = useState<boolean | null>(null);
  const [transcript, setTranscript] = useState<SessionTranscript | null>(null);
  const [aiStatus, setAiStatus] = useState(session.ai_status);
  const [aiError, setAiError] = useState<string | null>(null);
  const [audio, setAudio] = useState<{ blob: Blob; name: string } | null>(null);
  const [uploaded, setUploaded] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
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
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      granted = true;
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
    } catch {
      // Микрофон могли отдать, а рекордер (кодек) — нет: поток всё равно отпускаем,
      // иначе индикатор записи горит, а следующий `getUserMedia` теряет первый поток.
      releaseStream();
      recorder.current = null;
      setRecording(false);
      setErr(
        granted
          ? "Браузер не смог начать запись — загрузите файл записи."
          : "Браузер не даёт доступ к микрофону — загрузите файл записи.",
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
        setAiError(null);
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
        setAiError(null);
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
        <div className="row" style={{ marginTop: 8 }}>
          {recording ? (
            <button type="button" className="danger" onClick={stopRecording}>
              Остановить запись ({mmss(recSec)})
            </button>
          ) : (
            <button type="button" onClick={() => void startRecording()} disabled={!consent}>
              Начать запись
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
      </div>

      {err && (
        <p className="error" role="alert">
          {err}
        </p>
      )}
      {note && !err && <p className="small muted">{note}</p>}
    </details>
  );
}
