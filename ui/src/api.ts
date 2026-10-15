// Типизированный HTTP-клиент. Контракты — docs/04 §3.
const BASE = "";

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
  startScreening: () => req<ScreeningRun>("/api/v1/screenings", { method: "POST", body: "{}" }),
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
  adminAudit: () => req<{ items: AuditEvent[] }>("/api/v1/admin/audit"),
  prescribedScale: () => req<{ questionnaire_code: string }>("/api/v1/me/prescribed-scale"),
  setPrescribedScale: (patientId: string, scale: string) =>
    req<{ patient_id: string; questionnaire_code: string }>(`/api/v1/clinician/patients/${patientId}/prescribed-scale`, {
      method: "PUT",
      body: JSON.stringify({ scale }),
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
  prescribed_scale: string | null;
  last_assessment: {
    assessment_id: string;
    total_score: number;
    severity: string;
    triage: string;
    computed_at: string;
  } | null;
  history: { total_score: number; severity: string; triage: string; computed_at: string }[];
  trend: Timeline["trend"];
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