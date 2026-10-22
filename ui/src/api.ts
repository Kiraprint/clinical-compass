// Типизированный HTTP-клиент. Контракты — docs/04 §3.
// Пустой BASE = same-origin (веб, nginx-прокси). Для нативной сборки (Capacitor)
// хост задаётся на этапе сборки через VITE_API_BASE (см. mobile/ и release-android).
const BASE: string = import.meta.env.VITE_API_BASE ?? "";
// F9: web (same-origin) использует httpOnly cookies; native (cross-origin) —
// localStorage + Authorization header. cookies недоступны из JS → XSS-safe.
const USE_COOKIES: boolean = BASE === "";

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
  if (USE_COOKIES) return;  // F9: web — токен в httpOnly cookie, не в localStorage
  if (token) localStorage.setItem("cc_token", token);
  else localStorage.removeItem("cc_token");
}
export function getToken(): string | null {
  if (USE_COOKIES) return null;  // F9: web — токен недоступен из JS
  return localStorage.getItem("cc_token");
}
export function setRefreshToken(token: string | null) {
  if (USE_COOKIES) return;
  if (token) localStorage.setItem("cc_refresh", token);
  else localStorage.removeItem("cc_refresh");
}
export function getRefreshToken(): string | null {
  if (USE_COOKIES) return null;
  return localStorage.getItem("cc_refresh");
}

// Регистрируется App-ом: при неудачном refresh требуется логаут (страница входа).
let onAuthFail: (() => void) | null = null;
export function setOnAuthFail(fn: (() => void) | null) {
  onAuthFail = fn;
}

async function refreshAccessToken(): Promise<boolean> {
  // F9: web — refresh_token приходит из cc_refresh cookie (credentials: include).
  // native — из localStorage (body).
  const body: Record<string, string> = {};
  if (!USE_COOKIES) {
    const rt = getRefreshToken();
    if (!rt) return false;
    body.refresh_token = rt;
  }
  try {
    const resp = await fetch(`${BASE}/api/v1/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: USE_COOKIES ? undefined : JSON.stringify(body),
      credentials: "include",  // F9: cookies отправляются автоматически
    });
    if (!resp.ok) return false;
    // F4: сервер ротирует refresh — сохраняем новый (native), web — cookie обновляется.
    const respBody = (await resp.json()) as { access_token: string; refresh_token: string };
    if (!USE_COOKIES) {
      setToken(respBody.access_token);
      setRefreshToken(respBody.refresh_token);
    }
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
  // F9: credentials: include — httpOnly cookies (cc_access/cc_refresh) отправляются
  // автоматически для same-origin (web). Для cross-origin (native) — Authorization header.
  const resp = await fetch(`${BASE}${path}`, { ...init, headers, credentials: "include" });
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
/** Публичная конфигурация входа (CC_REQUIRE_OTP) — GET /auth/config. */
export interface AuthConfig {
  require_otp: boolean;
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
  authConfig: () => req<AuthConfig>("/api/v1/auth/config"),
  // F4: отзыв refresh-токена на сервере (best-effort, не блокирует UI).
  // F9: web-режим — refresh_token в cookie, body пустой; native — из localStorage.
  logout: () => {
    const rt = getRefreshToken();
    fetch(`${BASE}/api/v1/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: rt ? JSON.stringify({ refresh_token: rt }) : undefined,
      credentials: "include",  // F9: cookie очищается сервером
      keepalive: true,
    }).catch(() => {});
  },
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
  getMyAdherence: (days = 14) =>
    req<{ items: AdherenceRecord[]; today: AdherenceRecord | null }>(`/api/v1/me/adherence?days=${days}`),
  postChat: (content: string) =>
    req<ChatReply>("/api/v1/chat/thread", { method: "POST", body: JSON.stringify({ content }) }),
  getChat: () => req<{ items: ChatMessage[] }>("/api/v1/chat/thread"),
  clinicianPatients: (triage?: string) =>
    req<{ items: PatientCard[] }>(`/api/v1/clinician/patients${triage ? `?triage=${triage}` : ""}`),
  clinicianReport: (patientId: string) =>
    req<Report>(`/api/v1/clinician/patients/${patientId}/report`),
  assistantAsk: (query: string, limit = 5, patientId?: string) =>
    req<AssistantAnswer>("/api/v1/assistant/ask", {
      method: "POST",
      body: JSON.stringify({ query, limit, patient_id: patientId }),
    }),
  assistantStatus: () => req<{ collection: string; points: number }>("/api/v1/assistant/status"),
  // FR-21: сессии ассистента (треды) — persistent-контекст + мультипациент.
  assistantThreads: () => req<{ items: AssistantThread[] }>("/api/v1/assistant/threads"),
  assistantThread: (id: string) =>
    req<AssistantThreadDetail>(`/api/v1/assistant/threads/${id}`),
  assistantCreateThread: (title?: string, patientIds: string[] = []) =>
    req<AssistantThread>("/api/v1/assistant/threads", {
      method: "POST",
      body: JSON.stringify({ title: title ?? null, patient_ids: patientIds }),
    }),
  assistantUpdateThread: (
    id: string,
    body: { title?: string; patient_ids?: string[] },
  ) =>
    req<AssistantThread>(`/api/v1/assistant/threads/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  assistantDeleteThread: (id: string) =>
    req<void>(`/api/v1/assistant/threads/${id}`, { method: "DELETE" }),
  assistantPostMessage: (id: string, content: string, patientIds?: string[]) =>
    req<AssistantThreadAnswer>(`/api/v1/assistant/threads/${id}/messages`, {
      method: "POST",
      body: JSON.stringify(
        patientIds ? { content, patient_ids: patientIds } : { content },
      ),
    }),
  adminAudit: () => req<{ items: AuditEvent[] }>("/api/v1/admin/audit"),
  // --- Обогащение КБ (docs/15): ревью источников администратором ---
  adminKbHealth: () => req<KbHealth>("/api/v1/admin/kb/health"),
  adminKbRuns: (limit = 20) => req<{ runs: KbRun[] }>(`/api/v1/admin/kb/runs?limit=${limit}`),
  // status: "candidate" | "approved" | "rejected" | "excluded" | "superseded" | "" (все).
  // null = параметр не передаётся (сервер вернёт кандидатов по умолчанию).
  adminKbWorks: (
    params: { status?: string | null; kind?: string; limit?: number; offset?: number } = {},
  ) => {
    const q = new URLSearchParams();
    if (params.status !== null && params.status !== undefined) q.set("status", params.status);
    if (params.kind) q.set("kind", params.kind);
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    if (params.offset !== undefined) q.set("offset", String(params.offset));
    const query = q.toString();
    return req<KbWorksResp>(`/api/v1/admin/kb/works${query ? `?${query}` : ""}`);
  },
  adminKbApprove: (workId: string) =>
    req<KbWork>(`/api/v1/admin/kb/works/${encodeURIComponent(workId)}/approve`, { method: "POST" }),
  adminKbReject: (workId: string, reason: string) =>
    req<KbWork>(`/api/v1/admin/kb/works/${encodeURIComponent(workId)}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  // Персональные источники врача (docs/15 §6): исключение не удаляет источник из КБ.
  assistantSources: () => req<KbSourcesResp>("/api/v1/assistant/sources"),
  assistantSetSourceExcluded: (guidelineId: string, excluded: boolean) =>
    req<{ excluded_guideline_ids: string[] }>(
      `/api/v1/assistant/sources/${encodeURIComponent(guidelineId)}/exclude`,
      { method: "POST", body: JSON.stringify({ excluded }) },
    ),
  drugs: (q?: string) =>
    req<{ items: Drug[] }>(`/api/v1/drugs${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  drug: (drugId: string) => req<Drug>(`/api/v1/drugs/${encodeURIComponent(drugId)}`),
  prescribedScales: () => req<{ scales: { code: string; title: string }[] }>("/api/v1/me/prescribed-scales"),
  setPrescribedScales: (patientId: string, scales: string[]) =>
    req<{ patient_id: string; scales: string[] }>(`/api/v1/clinician/patients/${patientId}/prescribed-scales`, {
      method: "PUT",
      body: JSON.stringify({ scales }),
    }),
  createClinicianAssessment: (patientId: string, questionnaireCode: string, answers: Record<string, number>) =>
    req<Assessment>(`/api/v1/clinician/patients/${patientId}/assessments`, {
      method: "POST",
      body: JSON.stringify({ questionnaire_code: questionnaireCode, answers }),
    }),
  createAdverseEvent: (patientId: string, body: {
    event_text: string; severity: string; onset_date: string;
    suspected_drug?: string; action_taken?: string;
  }) =>
    req<Report["adverse_events"][number]>(`/api/v1/clinician/patients/${patientId}/adverse-events`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  recordAdherence: (body: { medication?: string; status: "taken" | "partial" | "missed"; note?: string }) =>
    req<Report["adherence"]["records"][number]>("/api/v1/me/adherence", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  clinicianChatList: (patientId: string) =>
    req<{ items: { id: string; sender: string; kind: string; content: string; created_at: string }[] }>(
      `/api/v1/clinician/patients/${patientId}/chat`),
  clinicianChatPost: (patientId: string, content: string) =>
    req<{ id: string; sender: string; kind: string; content: string; created_at: string }>(
      `/api/v1/clinician/patients/${patientId}/chat`, {
        method: "POST",
        body: JSON.stringify({ content }),
      }),
  importHistory: (patientId: string, text: string, templateName?: string) =>
    req<ImportHistoryResult>(`/api/v1/clinician/patients/${patientId}/import-history`, {
      method: "POST",
      body: JSON.stringify({ text, template_name: templateName }),
    }),
  clinicianVisits: (patientId: string) =>
    req<{ items: Visit[] }>(`/api/v1/clinician/patients/${patientId}/visits`),
  clinicianEmias: (patientId: string) =>
    req<EmiasSummary>(`/api/v1/clinician/patients/${patientId}/emias`),
  createVisit: (patientId: string, body: {
    visited_at: string; visit_type: "in_person" | "online" | "phone";
    duration_min?: number; notes: string;
  }) =>
    req<Visit>(`/api/v1/clinician/patients/${patientId}/visits`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteVisit: (patientId: string, visitId: string) =>
    req<void>(`/api/v1/clinician/patients/${patientId}/visits/${visitId}`, { method: "DELETE" }),
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
export interface AdherenceRecord {
  id: string;
  medication: string | null;
  status: string;
  note: string | null;
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
export interface ImportHistoryResult {
  template: string;
  fields_found: number;
  complaints: string | null;
  anamnesis: string | null;
  diagnosis_codes: string[];
  therapy: string[];
  scale_scores: string[];
  requires_confirmation: boolean;
}

export interface Visit {
  id: string;
  visited_at: string;
  visit_type: string;
  duration_min: number | null;
  notes: string;
  created_at: string;
}

export interface EmiasMedication {
  id: string;
  title: string;
  dosage: string | null;
  started_on: string | null;
  status: string;
  emias_id: string;
}

export interface EmiasSummary {
  linked: boolean;
  emias_patient_id: string | null;
  last_sync: string | null;
  encounters: number;
  medications: EmiasMedication[];
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
  adherence: {
    pct: number | null; total: number; taken: number; partial: number; missed: number;
    records: AdherenceRecord[];
  };
  adverse_events: {
    id: string; event_text: string; severity: string; onset_date: string;
    suspected_drug: string | null; action_taken: string | null; created_at: string;
  }[];
  diary: { id: string; content: string; mood: number | null; created_at: string }[];
  available_scales: {
    code: string; title: string; audience: "PATIENT" | "CLINICIAN";
    items: { key: string; text: string; max_value: number }[];
  }[];
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
/** FR-21: тред ассистента — persistent-контекст + область пациентов. */
export interface AssistantThread {
  id: string;
  title: string;
  patient_ids: string[];
  created_at: string | null;
  updated_at: string | null;
  message_count: number;
}
export interface AssistantThreadMessage {
  id: string;
  role: "CLINICIAN" | "ASSISTANT";
  content: string;
  citations: AssistantCitation[];
  topic: string | null;
  created_at: string | null;
}
export interface AssistantThreadDetail extends AssistantThread {
  messages: AssistantThreadMessage[];
}
export interface AssistantThreadAnswer {
  answer: string;
  citations: AssistantCitation[];
  topic: string | null;
  grounded: boolean;
  refused: boolean;
  refusal_code: string | null;
  skipped_patient_ids: string[];
  message_ids: string[];
}
/** Справочник препаратов (ГРЛС). Не ПДн: публичные факты рег. удостоверения. */
export interface Drug {
  drug_id: string;
  trade_name: string;
  inn: string;
  inn_en: string | null;
  form: string;
  reg_certificate: string;
  reg_date: string | null;
  revision_date: string | null;
  pharmacology: string;
  indications: string;
  dosage: string;
  contraindications: string;
  adverse_events: string;
  special_notes: string;
}

// --- Обогащение КБ (docs/15): прогоны, источники, вердикты ---
export type KbVerdictStatus = "met" | "not_met" | "insufficient_info" | "not_checked";
export type KbWorkKind = "article" | "guideline";
export type KbWorkStatus = "candidate" | "approved" | "rejected" | "excluded" | "superseded";

export interface KbRun {
  id: string;
  trigger: string;
  status: "running" | "ok" | "partial" | "failed";
  started_at: string | null;
  finished_at: string | null;
  stats: Record<string, unknown>;
  errors: string[];
  query_plan: Record<string, unknown>;
  versions: Record<string, unknown>;
}
export interface KbHealth {
  last_run: KbRun | null;
  counts: Record<string, number>;
}
export interface KbEvidence {
  quote: string;
  locator: string;
  source_url: string;
}
export interface KbVerdict {
  key: string;
  label: string;
  status: KbVerdictStatus;
  explanation: string;
  evidence: KbEvidence | null;
  confidence: "high" | "medium" | "low";
  manual_review: boolean;
  manual_review_reason: string;
}
export interface KbCriteria {
  version: string;
  decision: "include" | "exclude";
  reasons: string[];
  relevance_score: number;
  verdicts: KbVerdict[];
}
export interface KbDesign {
  design: string;
  confidence: "high" | "medium" | "low";
  rationale: string;
  matched_rules: string[];
}
export interface KbAppraisalItem {
  key: string;
  label: string;
  status: KbVerdictStatus;
  explanation: string;
  evidence: KbEvidence | null;
}
export interface KbAppraisal {
  tool: string;
  tool_label: string;
  applicable: boolean;
  reason: string;
  items: KbAppraisalItem[];
  overall: string;
  experimental: boolean;
  summary: string;
}
export interface KbWork {
  id: string;
  source: "pubmed" | "who";
  external_id: string;
  guideline_id: string;
  kind: KbWorkKind;
  status: KbWorkStatus;
  title: string;
  revision: string | null;
  year: number | null;
  doi: string | null;
  pmid: string | null;
  url: string | null;
  fulltext_url: string | null;
  license: string | null;
  journal: string | null;
  abstract: string | null;
  results: string | null;
  publication_types: string[];
  retraction: "none" | "retracted" | "correction" | "not_checked" | "unknown";
  design: KbDesign;
  criteria: KbCriteria;
  appraisal: KbAppraisal;
  chunk_count: number;
  indexed: boolean;
  first_seen_at: string | null;
  last_seen_at: string | null;
  published_at: string | null;
  approved_at: string | null;
  reject_reason: string | null;
  excluded?: boolean;
}
export interface KbWorksResp {
  works: KbWork[];
  counts: Record<string, number>;
}
export interface KbSourcesResp {
  sources: KbWork[];
  excluded_guideline_ids: string[];
}

/** Человекочитаемые названия дизайнов (зеркало api/app/enrichment/design.py). */
export const KB_DESIGN_LABELS: Record<string, string> = {
  network_meta_analysis: "Сетевой метаанализ",
  umbrella_review: "Обзор обзоров",
  systematic_review_meta_analysis: "Систематический обзор с метаанализом",
  meta_analysis: "Метаанализ",
  systematic_review: "Систематический обзор",
  rct: "Рандомизированное контролируемое исследование",
  cohort: "Когортное исследование",
  case_control: "Исследование случай–контроль",
  cross_sectional: "Поперечное исследование",
  guideline: "Клинический гайдлайн",
  narrative_review: "Нарративный обзор",
  other: "Дизайн не определён",
};