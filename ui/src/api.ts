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
  todayCheckin: () => req<CheckinState>("/api/v1/checkins/today"),
  // Идемпотентно: если сеанс на сегодня уже есть, сервер вернёт его (docs/16 §4).
  startCheckin: () => req<CheckinState>("/api/v1/checkins", { method: "POST", body: JSON.stringify({}) }),
  submitCheckin: (sessionId: string, answers: Record<string, number>) =>
    req<CheckinState>(`/api/v1/checkins/${encodeURIComponent(sessionId)}/answers`, {
      method: "POST",
      body: JSON.stringify({ answers }),
    }),
  getTimeline: (days = 90) => req<Timeline>(`/api/v1/me/timeline?days=${days}`),
  postDiary: verb<DiaryEntry>("POST")("/api/v1/diary"),
  getDiary: () => req<{ items: DiaryEntry[] }>("/api/v1/diary"),
  getMyAdherence: (days = 14) =>
    req<AdherenceSummary>(`/api/v1/me/adherence?days=${days}`),
  // FR-33: тренд чек-инов — только агрегат (среднее по пунктам ядра), без сырых ответов.
  getCheckinHistory: (days = 90) =>
    req<{ items: CheckinHistoryPoint[] }>(`/api/v1/checkins/history?days=${days}`),
  // FR-33: справочник ГРЛС по препаратам, назначенным пациенту (полный каталог — врачу).
  getMyDrugs: () => req<{ items: MyDrugReference[] }>("/api/v1/me/drugs"),
  // FR-28: нежелательные явления. Пациент сообщает, врач проверяет.
  myAdverseEvents: () => req<{ items: MyAdverseEvent[] }>("/api/v1/me/adverse-events"),
  // Тяжесть пациент не передаёт: её выставляет врач при проверке (severity — колонка врача).
  reportAdverseEvent: (body: {
    event_text: string; onset_date: string; medication_id?: string;
  }) =>
    req<MyAdverseEvent>("/api/v1/me/adverse-events", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  adverseQueue: (status?: AdverseStatus) =>
    req<{ items: ClinicianAdverseEvent[] }>(
      `/api/v1/clinician/adverse-events${status ? `?status=${status}` : ""}`,
    ),
  verifyAdverseEvent: (patientId: string, eventId: string, body: {
    status: "confirmed" | "rejected";
    severity?: AdverseSeverity;
    suspected_drug?: string;
    medication_id?: string;
    drug_id?: string;
    action_taken?: string;
    verification_note?: string;
  }) =>
    req<ClinicianAdverseEvent>(
      `/api/v1/clinician/patients/${patientId}/adverse-events/${eventId}`,
      { method: "PATCH", body: JSON.stringify(body) },
    ),
  postChat: (content: string) =>
    req<ChatReply>("/api/v1/chat/thread", { method: "POST", body: JSON.stringify({ content }) }),
  getChat: () => req<{ items: ChatMessage[] }>("/api/v1/chat/thread"),
  clinicianPatients: (triage?: string) =>
    req<{ items: PatientCard[] }>(`/api/v1/clinician/patients${triage ? `?triage=${triage}` : ""}`),
  clinicianReport: (patientId: string) =>
    req<Report>(`/api/v1/clinician/patients/${patientId}/report`),
  // FR-22: контакты пациента. Врач — roster+consent, пациент — только свои.
  clinicianContacts: (patientId: string) =>
    req<PatientContacts>(`/api/v1/clinician/patients/${patientId}/contacts`),
  clinicianContactsUpdate: (patientId: string, body: ContactPatch) =>
    req<PatientContacts>(`/api/v1/clinician/patients/${patientId}/contacts`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  // FR-37: анамнез пациента — вводные + подтверждённый текст.
  clinicianAnamnesis: (patientId: string) =>
    req<Anamnesis>(`/api/v1/clinician/patients/${patientId}/anamnesis`),
  clinicianAnamnesisSave: (patientId: string, body: { brief: string; text?: string | null }) =>
    req<Anamnesis>(`/api/v1/clinician/patients/${patientId}/anamnesis`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  clinicianAnamnesisDraft: (patientId: string, brief?: string) =>
    req<AnamnesisDraft>(`/api/v1/clinician/patients/${patientId}/anamnesis/draft`, {
      method: "POST",
      body: JSON.stringify({ brief: brief?.trim() ? brief : null }),
    }),
  myContacts: () => req<PatientContacts>("/api/v1/profile/contacts"),
  myContactsUpdate: (body: ContactPatch) =>
    req<PatientContacts>("/api/v1/profile/contacts", { method: "PATCH", body: JSON.stringify(body) }),
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
  prescribedScales: () =>
    req<{
      scales: {
        code: string;
        title: string;
        prescribed_at: string | null;
        // Не null — по этому назначению уже есть завершённая оценка.
        completed_at: string | null;
      }[];
    }>("/api/v1/me/prescribed-scales"),
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
  recordAdherence: (body: {
    medication_id?: string; medication?: string;
    status: "taken" | "partial" | "missed" | "not_scheduled";
    partial_kind?: PartialKind; note?: string;
  }) =>
    req<Report["adherence"]["records"][number]>("/api/v1/me/adherence", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // FR-32: карточка врача в личном кабинете (ФИО, e-mail, роли).
  clinicianMe: () => req<ClinicianProfile>("/api/v1/clinician/me"),
  clinicianChats: () => req<{ items: PatientChat[] }>("/api/v1/clinician/chats"),
  clinicianChatList: (patientId: string) =>
    req<{ items: PatientChatMessage[] }>(`/api/v1/clinician/patients/${patientId}/chat`),
  clinicianChatPost: (patientId: string, content: string) =>
    req<PatientChatMessage>(`/api/v1/clinician/patients/${patientId}/chat`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  // FR-31: анализ переписки с пациентом. 503 CHAT_INSIGHTS_DISABLED — фича выключена.
  chatInsights: (patientId: string, status?: ChatInsight["status"]) =>
    req<{ items: ChatInsight[] }>(
      `/api/v1/clinician/patients/${patientId}/chat/insights${status ? `?status=${status}` : ""}`,
    ),
  analyzeChat: (patientId: string) =>
    req<ChatInsightRun>(`/api/v1/clinician/patients/${patientId}/chat/analyze`, {
      method: "POST",
    }),
  decideChatInsight: (patientId: string, insightId: string, status: "accepted" | "rejected") =>
    req<ChatInsight>(`/api/v1/clinician/patients/${patientId}/chat/insights/${insightId}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
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

  clinicianMedications: (patientId: string, params: { status?: string; active_only?: boolean } = {}) => {
    const qs = new URLSearchParams();
    if (params.status) qs.set("status", params.status);
    if (params.active_only) qs.set("active_only", "true");
    const suffix = qs.toString() ? `?${qs}` : "";
    return req<{ items: PatientMedication[] }>(
      `/api/v1/clinician/patients/${patientId}/medications${suffix}`);
  },
  createMedication: (patientId: string, body: {
    title: string; dosage?: string; started_on?: string; ended_on?: string;
    status?: string; notes?: string; times_per_day?: number | null;
  }) =>
    req<PatientMedication>(`/api/v1/clinician/patients/${patientId}/medications`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateMedication: (patientId: string, medId: string, body: {
    dosage?: string | null; status?: string; ended_on?: string | null; notes?: string | null;
    times_per_day?: number | null;
  }) =>
    req<PatientMedication>(`/api/v1/clinician/patients/${patientId}/medications/${medId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  stopMedication: (patientId: string, medId: string, endedOn?: string) =>
    req<PatientMedication>(`/api/v1/clinician/patients/${patientId}/medications/${medId}/stop`, {
      method: "POST",
      body: JSON.stringify({ ended_on: endedOn ?? null }),
    }),
  deleteMedication: (patientId: string, medId: string) =>
    req<void>(`/api/v1/clinician/patients/${patientId}/medications/${medId}`, { method: "DELETE" }),
  clinicianHistory: (patientId: string, params: { types?: string; limit?: number; before?: string } = {}) => {
    const qs = new URLSearchParams();
    if (params.types) qs.set("types", params.types);
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.before) qs.set("before", params.before);
    const suffix = qs.toString() ? `?${qs}` : "";
    return req<PatientHistory>(`/api/v1/clinician/patients/${patientId}/history${suffix}`);
  },
  // Очередь эскалаций (docs/16 §3): open — работа, resolved — история разбора, all — оба.
  openEscalations: (status: "open" | "resolved" | "all" = "open") =>
    req<{ items: Escalation[] }>(`/api/v1/escalations?status=${status}`),
  // Закрытие сигнала вручную (аудит на сервере). Повтор → 409 ESCALATION_ALREADY_RESOLVED,
  // чужая/отсутствующая эскалация → 404; в обоих случаях строку из очереди убираем.
  resolveEscalation: (id: string, resolution: string | null = null) =>
    verb<EscalationResolved>("POST")(`/api/v1/escalations/${encodeURIComponent(id)}/resolve`)({
      resolution,
    }),
  // FR-24: напоминания врача. Список — свои (roster/IDOR-гейт на сервере).
  clinicianReminders: (due?: "overdue" | "today" | "week") =>
    req<RemindersResponse>(
      `/api/v1/clinician/reminders?status=open${due ? `&due=${due}` : ""}`,
    ),
  clinicianPatientReminders: (patientId: string, status = "open") =>
    req<RemindersResponse>(
      `/api/v1/clinician/patients/${patientId}/reminders?status=${status}`,
    ),
  createReminder: (body: {
    patient_id: string;
    kind: ClinicianReminder["kind"];
    note?: string | null;
    due_at: string;
  }) =>
    req<ClinicianReminder>("/api/v1/clinician/reminders", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  patchReminder: (id: string, body: ReminderPatch) =>
    req<ClinicianReminder>(`/api/v1/clinician/reminders/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  doneReminder: (id: string) =>
    req<ClinicianReminder>(`/api/v1/clinician/reminders/${id}/done`, { method: "POST" }),
  cancelReminder: (id: string) =>
    req<ClinicianReminder>(`/api/v1/clinician/reminders/${id}/cancel`, { method: "POST" }),
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
  // Сумма максимумов предъявленных пациенту пунктов (PHQ-9 → 24, GAD-7 → 21,
  // HCL-32 → 32, PSS-10 → 40): верхняя граница графика тренда по этой шкале.
  max_total: number;
  items: { key: string; text: string; max_value: number; anchors: string[] }[];
}
export interface Assessment {
  assessment_id: string;
  questionnaire_code: string;
  total_score: number;
  severity: string;
  triage: string;
  risk_flags: { kind: string; item_key: string | null }[];
  // FR-34: пункты шкалы, которые пациенту не задавались (PHQ-9 q9) — шкала неполная.
  omitted_items?: string[];
  computed_at: string;
  escalation?: {
    id: string;
    kind: string;
    help_resources: { title: string; phone: string }[];
  };
}
// Адаптивный чек-ин, docs/16 §4. Шаг — это один экран: ядро, безопасность или модули.
export interface CheckinStepItem {
  id: string;
  domain: string;
  text: string;
  kind: string; // scale | binary | gate
  anchors: string[];
  emoji: string[];
  max_value: number;
}
export interface CheckinModule {
  code: string;
  title: string;
  included: boolean;
  reason: string;
}
// phase: core | safety | modules | done; step.kind повторяет phase.
export interface CheckinState {
  date: string;
  rules_version: string;
  status: string; // not_started | in_progress | completed
  session_id: string | null;
  phase: string | null;
  step: { kind: string | null; items: CheckinStepItem[] };
  answers: Record<string, number>;
  progress: {
    answered: number;
    planned: number;
    // v2.0.0: задавался ли HR4 и на каком основании (docs/16 §2).
    safety_gate: boolean;
    safety_gate_reason: string;
  };
  // true — сегодня задавался вопрос о риске после ядра.
  safety_gate: boolean;
  safety: string; // none | review | signal | unassessed
  modules: CheckinModule[];
  completed_at: string | null;
  // true — сигнал ушёл закреплённому врачу; false — врача нет (docs/16 §3).
  clinician_notified: boolean;
}
export interface Timeline {
  runs: {
    total_score: number; severity: string; triage: string; computed_at: string;
    // FR-33: код шкалы — у пациента несколько назначенных, тренд строится по каждой.
    questionnaire_code?: string;
    // Верх шкалы (сумма максимумов пунктов): 24/21/32/40/52 — граница графика.
    // Нет у клиницистского отчёта (Report.history) — график берёт верх по наблюдениям.
    max_total?: number;
  }[];
  trend: { moving_avg: number[]; delta_7: number | null; delta_30: number | null; slope: number | null };
}

export type MoodScale = "0-4" | "1-10" | null;
export interface DiaryEntry {
  id: string;
  content: string;
  mood_scale: MoodScale;
  mood: number | null; // 0..4 при mood_scale="0-4"; историчные записи — 1..10
  sleep_h: number | null;
  tags: string[];
  created_at: string;
}
// Уточнение «частично» (FR-27): доля принятого приёма.
export type PartialKind =
  | "quarter" | "half" | "three_quarters"
  | "one_of_two" | "two_of_three"
  | "less_than_prescribed" | "self_changed";

export interface AdherenceRecord {
  id: string;
  medication_id: string | null;
  medication: string | null;
  status: "taken" | "partial" | "missed" | "not_scheduled";
  partial_kind: PartialKind | null;
  note: string | null;
  created_at: string;
}

// FR-27: приверженность по назначению — знаменатель из режима (times_per_day).
export interface AdherenceMedication {
  id: string;
  title: string;
  status: string;
  times_per_day: number | null;
  started_on: string | null;
  ended_on: string | null;
  active_days: number;
  expected: number;
  taken: number;
  partial: number;
  missed: number;
  not_scheduled: number;
  score: number;
  pct: number | null;
}
export interface AdherenceSummary {
  days: number;
  window: { from: string; to: string };
  // Общий процент — weighted: сумма score / сумма expected (не среднее процентов).
  summary: { pct: number | null; score: number; expected: number };
  medications: AdherenceMedication[];
  active_medications: {
    id: string; title: string; times_per_day: number | null; started_on: string | null;
  }[];
  total: number;
  taken: number;
  partial: number;
  missed: number;
  not_scheduled: number;
  excluded: { count: number; items: AdherenceRecord[] };
  items: AdherenceRecord[];
  today: AdherenceRecord | null;
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
  // FR-34: не более одного автоответа в сутки (Europe/Moscow); иначе null.
  reply: (ChatMessage & { help_resources: { title: string; phone: string }[] }) | null;
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
  // FR-22: телефон только маской (+7•••123-45-67); полное значение — endpoint контактов.
  phone: string | null;
  birth_date: string | null;
  age: number | null;
}
export interface PatientContacts {
  phone: string | null;
  birth_date: string | null;
  age: number | null;
  adult: boolean | null;
  // FR-33: только self-service (/profile/contacts); витрины врача их не отдают.
  fio?: string | null;
  email?: string;
}
export interface ContactPatch {
  phone?: string | null;
  birth_date?: string | null;
}
// FR-32: свой профиль врача для личного кабинета.
export interface ClinicianProfile {
  fio: string | null;
  email: string;
  role: string;
  roles: string[];
}
// FR-24: личное напоминание врача по пациенту. note — свободный текст (может
// содержать ПДн), в списке приходит расшифрованным (доступ только владельцу).
export interface ClinicianReminder {
  id: string;
  patient_id: string;
  patient_fio: string;
  kind: "call" | "write" | "appointment" | "custom";
  kind_label: string;
  note: string | null;
  due_at: string;
  status: "open" | "done" | "cancelled";
  due_state: "overdue" | "today" | "soon" | "later" | null;
  overdue_minutes: number;
  done_at: string | null;
  notified_at: string | null;
  created_at: string;
}
export interface RemindersResponse {
  items: ClinicianReminder[];
  overdue: number;
}
export interface ReminderPatch {
  note?: string | null;
  due_at?: string;
  kind?: ClinicianReminder["kind"];
}

// FR-29: сводка переписки врача с пациентом для списка на экране «Чаты».
// preview — уже расшифрованная сервером обрезка (≤120 символов), не для логов.
export interface PatientChat {
  patient_id: string;
  fio: string | null;
  last_at: string;
  last_sender: "PATIENT" | "SYSTEM" | "CLINICIAN" | null;
  preview: string;
  message_count: number;
}
export interface PatientChatMessage {
  id: string;
  sender: string;
  kind: string;
  content: string;
  created_at: string;
}

/** FR-31: факт из переписки — черновик до решения врача. */
export interface ChatInsight {
  id: string;
  patient_id: string;
  kind: "symptom" | "medication" | "adherence" | "sleep" | "substance" | "risk" | "other";
  text: string;
  confidence: number | null;
  source_message_ids: string[];
  status: "pending" | "accepted" | "rejected";
  created_at: string;
  reviewed_at: string | null;
}
export interface ChatInsightRun {
  created: number;
  skipped: number;
  items: ChatInsight[];
}
/** В карточке — только принятые факты, без source_message_ids. */
export interface AcceptedChatInsight {
  id: string;
  kind: ChatInsight["kind"];
  text: string;
  created_at: string;
  reviewed_at: string | null;
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

export type MedicationSource = "emias" | "clinician";

export interface PatientMedication {
  id: string;
  title: string;
  dosage: string | null;
  started_on: string | null;
  ended_on: string | null;
  times_per_day: number | null;
  status: string;
  notes: string | null;
  source: MedicationSource;
  clinician_id: string | null;
  editable: boolean;
  created_at: string;
}

export type HistoryType = "visit" | "medication" | "screening" | "adverse";

export interface PatientHistoryItem {
  type: HistoryType;
  at: string;
  title: string;
  summary: string;
  ref_id: string;
  source: MedicationSource | "patient";
}

export interface PatientHistory {
  items: PatientHistoryItem[];
  next_before: string | null;
  types: HistoryType[];
}

export interface EmiasMedication {
  id: string;
  title: string;
  dosage: string | null;
  started_on: string | null;
  status: string;
}

export interface EmiasSummary {
  linked: boolean;
  enabled: boolean;
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
    // FR-34: kind="NOT_ASSESSED" — пункт 9 не задавался, риск не оценён.
    risk_flags?: { kind: string; item_key: string | null }[];
    omitted_items?: string[];
    // Шкала, из которой взят omitted_items (последняя с suicidal_item): после
    // заполненного GAD-7 пропуск q9 всё ещё относится к PHQ-9.
    omitted_scale?: string | null;
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
    days: number;
    window: { from: string; to: string };
    // weighted: сумма score / сумма expected по назначениям с режимом.
    pct: number | null; score: number; expected: number;
    total: number; taken: number; partial: number; missed: number; not_scheduled: number;
    medications: AdherenceMedication[];
    excluded_count: number;
    records: AdherenceRecord[];
  };
  adverse_events: ClinicianAdverseEvent[];
  diary: { id: string; content: string; mood: number | null; mood_scale: MoodScale; created_at: string }[];
  available_scales: {
    code: string; title: string; audience: "PATIENT" | "CLINICIAN";
    items: { key: string; text: string; max_value: number; anchors: string[] }[];
  }[];
  open_escalations: { id: string; kind: string; notified_at: string | null; checkin_session_id?: string | null }[];
  // docs/16 §3: ответы чек-ина, состояние риска и основания включённых модулей.
  checkin: {
    rules_version: string | null;
    recent: {
      session_id: string; date: string; status: string; phase: string | null;
      safety: "none" | "review" | "signal" | "unassessed"; rules_version: string;
      answers: Record<string, number>;
      modules: { code: string; included: boolean; reason: string }[];
      completed_at: string | null;
    }[];
  };
  // FR-31: подтверждённые врачом факты из переписки (черновики, не диагноз).
  chat_insights: AcceptedChatInsight[];
  // FR-37: анамнез; null — нет активного согласия пациента.
  anamnesis: Anamnesis | null;
  // FR-22: null, если у пациента нет активного согласия (152-ФЗ).
  contacts: { phone: string | null; birth_date: string | null; age: number | null; adult: boolean | null } | null;
}
export interface Anamnesis {
  patient_id: string;
  brief: string | null;
  text: string | null;
  updated_at: string | null;
  updated_by: string | null;
}
export interface AnamnesisDraft {
  draft: string;
  /** Счётчики категорий, замаскированных перед отправкой в LLM (без значений). */
  masked_report: { applied: boolean; counts: Record<string, number> };
}
export interface Escalation {
  id: string;
  patient_id: string;
  patient_fio: string | null;
  kind: "CHECKIN_RISK" | "CHECKIN_RISK_URGENT" | "SUICIDAL" | "SEVERE" | string;
  safety: "none" | "review" | "signal" | "unassessed" | null;
  checkin_session_id: string | null;
  notified_at: string | null;
  /** Пациент закреплён за врачом: только тогда карточка открывается (иначе 403). */
  in_roster: boolean;
  /** Ответы риска из чек-ина: {"HR4":1,"RS1":0,...}; на которые не ответили — ключа нет. */
  risk_answers: Record<string, number> | null;
  created_at: string | null;
  /** Заполнены только у закрытых сигналов (status=resolved|all). */
  resolved_at: string | null;
  resolution: string | null;
}
/** Ответ POST /escalations/{id}/resolve. */
export interface EscalationResolved {
  id: string;
  resolved_at: string;
  resolution: string | null;
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
  source_urls: string[];
}

// --- FR-28: нежелательные явления ---
export type AdverseSeverity = "mild" | "moderate" | "severe";
export type AdverseStatus = "pending" | "confirmed" | "rejected";
/** Фрагмент справочника ГРЛС, привязанный врачом к событию. */
export interface DrugReference {
  drug_id: string;
  trade_name: string;
  adverse_events: string;
}
/** Событие глазами пациента: без врачебных заметок. */
export interface MyAdverseEvent {
  id: string;
  event_text: string;
  // FR-33/34: пациент тяжесть не указывает — NULL до проверки врачом.
  severity: AdverseSeverity | null;
  onset_date: string;
  status: AdverseStatus;
  status_label: string;
  medication_id: string | null;
  medication_title: string | null;
  verified_at: string | null;
  created_at: string;
}
/** FR-33: точка тренда чек-инов — агрегат 0–4, без расшифровки ответов. */
export interface CheckinHistoryPoint {
  date: string;
  wellbeing: number;
  safety: string;
}
/** FR-33: препарат из назначений пациента + карточка ГРЛС (если нашлась по названию). */
export interface MyDrugReference {
  medication_id: string;
  title: string;
  dosage: string | null;
  status: string;
  status_label: string;
  times_per_day: number | null;
  drug: {
    drug_id: string;
    trade_name: string;
    inn: string | null;
    form: string | null;
    pharmacology: string | null;
    indications: string | null;
    dosage: string | null;
    contraindications: string | null;
    adverse_events: string | null;
    special_notes: string | null;
    source_urls: string[];
  } | null;
}
/** Событие для врача: проверка, назначение и ссылка на справочник. */
export interface ClinicianAdverseEvent {
  id: string;
  patient_id: string;
  patient_fio: string | null;
  event_text: string;
  // Пациент может сообщить НЯ без оценки тяжести (UxPatient: severity nullable).
  severity: AdverseSeverity | null;
  onset_date: string;
  status: AdverseStatus;
  reported_by: "clinician" | "patient";
  suspected_drug: string | null;
  action_taken: string | null;
  medication_id: string | null;
  medication_title: string | null;
  drug_id: string | null;
  drug_trade_name: string | null;
  reference: DrugReference | null;
  verification_note: string | null;
  verified_by: string | null;
  verified_at: string | null;
  created_at: string;
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