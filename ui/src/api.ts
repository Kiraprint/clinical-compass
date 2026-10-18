// Типизированный HTTP-клиент. Контракты — docs/04 §3.
// Пустой BASE = same-origin (веб, nginx-прокси). Для нативной сборки (Capacitor)
// хост задаётся на этапе сборки через VITE_API_BASE (см. mobile/ и release-android).
const BASE: string = import.meta.env.VITE_API_BASE ?? "";

export class ApiError extends Error {
  status: number;
  code: string;
  detail: string;
  constructor(status: number, code: string, detail: string) {
    super(detail);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem("cc_token", token);
  else localStorage.removeItem("cc_token");
}
export function getToken(): string | null {
  return localStorage.getItem("cc_token");
}
export function setRefreshToken(token: string | null) {
  if (token) localStorage.setItem("cc_refresh", token);
  else localStorage.removeItem("cc_refresh");
}
export function getRefreshToken(): string | null {
  return localStorage.getItem("cc_refresh");
}

// Регистрируется App-ом: при неудачном refresh требуется логаут (страница входа).
let onAuthFail: (() => void) | null = null;
export function setOnAuthFail(fn: (() => void) | null) {
  onAuthFail = fn;
}

async function refreshAccessToken(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;
  try {
    const resp = await fetch(`${BASE}/api/v1/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!resp.ok) return false;
    const body = (await resp.json()) as { access_token: string };
    setToken(body.access_token);
    return true;
  } catch {
    return false;
  }
}

let refreshing = false;

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (init.body) headers["Content-Type"] = "application/json";
  const resp = await fetch(`${BASE}${path}`, { ...init, headers });
  // 401 после истечения access-токена: одна попытка refresh, затем повтор исходного запроса.
  if (resp.status === 401 && !refreshing) {
    refreshing = true;
    try {
      if (await refreshAccessToken()) {
        return req<T>(path, init);
      }
    } finally {
      refreshing = false;
    }
    onAuthFail?.();
  }
  if (resp.status === 204) return undefined as T;
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new ApiError(resp.status, body.code ?? "UNKNOWN", body.detail ?? resp.statusText);
  }
  return body as T;
}

const verb =
  <T,>(method: string) =>
  (path: string) =>
  (data?: unknown): Promise<T> =>
    req<T>(path, { method, body: data !== undefined ? JSON.stringify(data) : undefined });

export interface RegisterResp {
  id: string;
  email: string;
  role: string;
  totp_secret?: string;
}
export interface LoginResp {
  access_token: string;
  refresh_token: string;
  role: string;
}
export interface ScreeningRun {
  run_id: string;
  questionnaire_code: string;
  mode: string;
  started_at: string;
}

export const api = {
  register: verb<RegisterResp>("POST")("/api/v1/auth/register"),
  login: verb<LoginResp>("POST")("/api/v1/auth/login"),
  getConsent: () => req<Consent>("/api/v1/consent"),
  postConsent: verb<Consent>("POST")("/api/v1/consent"),
  revokeConsent: () => req<void>("/api/v1/consent/revoke", { method: "POST" }),
  getQuestionnaires: () => req<Questionnaire[]>("/api/v1/questionnaires"),
  startScreening: (questionnaireCode?: string) =>
    req<ScreeningRun>("/api/v1/screenings", {
      method: "POST",
      body: JSON.stringify(questionnaireCode ? { questionnaire_code: questionnaireCode } : {}),
    }),
  submitAnswers: (runId: string, answers: Record<string, number>) =>
    req<void>(`/api/v1/runs/${runId}/answers`, {
      method: "POST",
      body: JSON.stringify({ answers }),
    }),
  finishRun: (runId: string) =>
    req<Assessment>(`/api/v1/runs/${runId}/finish`, { method: "POST" }),
  abandonScreening: () =>
    req<{ abandoned: boolean }>("/api/v1/screenings/abandon", { method: "POST" }),
  getTodayCheckin: () => req<CheckinToday>("/api/v1/checkins/today"),
  completeCheckin: (answers: Record<string, number>) =>
    req<{ run_id: string }>("/api/v1/checkins", { method: "POST", body: JSON.stringify({ answers }) }),
  getTimeline: (days = 90) => req<Timeline>(`/api/v1/me/timeline?days=${days}`),
  postDiary: verb<DiaryEntry>("POST")("/api/v1/diary"),
  getDiary: () => req<{ items: DiaryEntry[] }>("/api/v1/diary"),
  postChat: (content: string) =>
    req<ChatReply>("/api/v1/chat/thread", { method: "POST", body: JSON.stringify({ content }) }),
  getChat: () => req<{ items: ChatMessage[] }>("/api/v1/chat/thread"),
  clinicianPatients: (triage?: string) =>
    req<{ items: PatientCard[] }>(`/api/v1/clinician/patients${triage ? `?triage=${triage}` : ""}`),
  clinicianReport: (patientId: string) =>
    req<Report>(`/api/v1/clinician/patients/${patientId}/report`),
  assistantAsk: (query: string, topic?: string, limit = 5, patientId?: string) =>
    req<AssistantAnswer>("/api/v1/assistant/ask", {
      method: "POST",
      body: JSON.stringify({ query, topic: topic || undefined, limit, patient_id: patientId }),
    }),
  assistantStatus: () => req<{ collection: string; points: number }>("/api/v1/assistant/status"),
  adminAudit: () => req<{ items: AuditEvent[] }>("/api/v1/admin/audit"),
  prescribedScales: () => req<{ scales: { code: string; title: string }[] }>("/api/v1/me/prescribed-scales"),
  setPrescribedScales: (patientId: string, scales: string[]) =>
    req<{ patient_id: string; scales: string[] }>(`/api/v1/clinician/patients/${patientId}/prescribed-scales`, {
      method: "PUT",
      body: JSON.stringify({ scales }),
    }),
  createAdverseEvent: (patientId: string, body: {
    event_text: string; severity: string; onset_date: string;
    suspected_drug?: string; action_taken?: string;
  }) =>
    req<Report["adverse_events"][number]>(`/api/v1/clinician/patients/${patientId}/adverse-events`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  vapidPublicKey: () => req<{ public_key: string }>("/api/v1/push/vapid-public-key"),
  subscribePush: (endpoint: string, keys: { p256dh: string; auth: string }) =>
    req<{ subscribed: boolean }>("/api/v1/push/subscribe", {
      method: "POST",
      body: JSON.stringify({ endpoint, keys }),
    }),
  getNotifications: () => req<{ items: NotificationItem[]; unread: number }>("/api/v1/me/notifications"),
  readNotifications: (ids?: string[]) =>
    req<{ read: boolean }>("/api/v1/me/notifications/read", {
      method: "POST",
      body: JSON.stringify({ ids: ids ?? null }),
    }),
  registerDeviceToken: (token: string, platform: string = "android") =>
    req<{ registered: boolean }>("/api/v1/push/device-token", {
      method: "POST",
      body: JSON.stringify({ token, platform }),
    }),
  fcmStatus: () =>
    req<{ window_active: boolean; remaining_seconds: number }>("/api/v1/push/fcm/status"),
  fcmUpgrade: (token: string, platform: string = "android") =>
    req<{ upgraded: boolean }>("/api/v1/push/fcm/upgrade", {
      method: "POST",
      body: JSON.stringify({ token, platform }),
    }),
};

// --- Типы (зеркало контрактов docs/04 §3.2) ---
export interface Consent {
  id: string;
  version: number;
  signed_at: string;
  revoked_at: string | null;
}
export interface Questionnaire {
  code: string;
  title: string;
  items: { key: string; text: string; max_value: number }[];
}
export interface Assessment {
  assessment_id: string;
  questionnaire_code: string;
  total_score: number;
  severity: string;
  triage: string;
  risk_flags: { kind: string; item_key: string | null }[];
  computed_at: string;
  escalation?: {
    id: string;
    kind: string;
    help_resources: { title: string; phone: string }[];
  };
}
export interface CheckinToday {
  items: { key: string; text: string; max_value: number }[];
  done: boolean;
}
export interface Timeline {
  runs: { total_score: number; severity: string; triage: string; computed_at: string }[];
  trend: { moving_avg: number[]; delta_7: number | null; delta_30: number | null; slope: number | null };
}
export interface DiaryEntry {
  id: string;
  content: string;
  mood: number | null;
  sleep_h: number | null;
  tags: string[];
  created_at: string;
}
export interface ChatMessage {
  id: string;
  sender: string;
  kind: string;
  content: string;
  created_at: string;
}
export interface ChatReply {
  message: ChatMessage;
  reply: ChatMessage & { help_resources: { title: string; phone: string }[] };
}
export interface PatientCard {
  patient_id: string;
  email: string;
  fio: string | null;
  last_triage: string | null;
  last_score: number | null;
  last_severity: string | null;
  last_assessment_at: string | null;
  open_escalations: number;
}
export interface Report {
  patient_id: string;
  email: string;
  fio: string | null;
  prescribed_scales: string[];
  last_assessment: {
    assessment_id: string;
    total_score: number;
    severity: string;
    triage: string;
    computed_at: string;
  } | null;
  history: { total_score: number; severity: string; triage: string; computed_at: string }[];
  trend: Timeline["trend"];
  routing: {
    category: "REQUIRED" | "DESIRABLE" | "NONE" | null;
    label: string;
    target: string;
    reason: string;
  } | null;
  adverse_events: {
    id: string; event_text: string; severity: string; onset_date: string;
    suspected_drug: string | null; action_taken: string | null; created_at: string;
  }[];
  diary: { id: string; content: string; mood: number | null; created_at: string }[];
  available_scales: { code: string; title: string }[];
  open_escalations: { id: string; kind: string; notified_at: string | null }[];
}
export interface AuditEvent {
  id: number;
  actor_id: string | null;
  action: string;
  object_type: string;
  object_id: string | null;
  ip: string | null;
  occurred_at: string;
}
export interface NotificationItem {
  id: string;
  kind: string;
  title: string;
  body: string;
  patient_id: string | null;
  escalation_id: string | null;
  read: boolean;
  created_at: string;
}

export interface AssistantCitation {
  source_title: string;
  guideline_id: string;
  topic: string;
  score: number;
}
export interface AssistantAnswer {
  answer: string;
  grounded: boolean;
  refused: boolean;
  refusal_code: string | null;
  citations: AssistantCitation[];
}