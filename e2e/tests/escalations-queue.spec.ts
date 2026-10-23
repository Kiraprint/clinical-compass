// docs/16 §3: сигнал риска из чек-ина попадает в общую очередь эскалаций. Врач видит
// ответы риска; карточка открывается только для своего (roster) пациента, чужой сигнал
// можно только закрыть — и он исчезает из очереди.
import { expect, request, test, type Page } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, freshPatientEmail, login, registerPatient, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";

type QueueItem = {
  id: string;
  patient_id: string;
  patient_fio: string | null;
  kind: string;
  safety: string | null;
  checkin_session_id: string | null;
  in_roster: boolean;
  risk_answers: Record<string, number> | null;
};

type QueueBody = { items: QueueItem[] };

// Web-клиент держит токен в httpOnly cookie: API-контекст получает cookies из браузера.
const apiAs = async (page: Page) =>
  request.newContext({ baseURL: BASE, storageState: await page.context().storageState() });

// Логин без UI: нужен токен админа (ростер) и id врача из JWT (sub).
const loginApi = async (email: string) => {
  const ctx = await request.newContext({ baseURL: BASE });
  const resp = await ctx.post("/api/v1/auth/login", {
    data: { email, password: DEMO_PASSWORD, otp: totp(DEMO_TOTP) },
  });
  expect(resp.status()).toBe(200);
  const token: string = (await resp.json()).access_token;
  await ctx.setExtraHTTPHeaders({ Authorization: `Bearer ${token}` });
  const claims = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
  return { ctx, userId: claims.sub as string };
};

// Риск-чек-ин: HR4=«Да», RS1–RS3=«Нет» → safety=signal и запись в общей очереди.
const riskCheckin = async (page: Page): Promise<string> => {
  const asPatient = await apiAs(page);
  const started = await asPatient.post("/api/v1/checkins", { data: {} });
  expect(started.status()).toBe(201);
  const core = (await started.json()) as {
    session_id: string;
    phase: string;
    step: { kind: string; items: { id: string }[] };
  };
  expect(core.step.kind).toBe("core");

  const coreAnswers: Record<string, number> = Object.fromEntries(core.step.items.map((i) => [i.id, 0]));
  coreAnswers.HR4 = 1;
  const safetyStep = await asPatient.post(`/api/v1/checkins/${core.session_id}/answers`, {
    data: { answers: coreAnswers },
  });
  expect(safetyStep.status()).toBe(200);
  const afterCore = (await safetyStep.json()) as { phase: string; step: { items: { id: string }[] } };
  expect(afterCore.phase).toBe("safety");

  const safetyAnswers: Record<string, number> = Object.fromEntries(
    afterCore.step.items.map((i) => [i.id, 0]),
  );
  const done = await asPatient.post(`/api/v1/checkins/${core.session_id}/answers`, {
    data: { answers: safetyAnswers },
  });
  expect(done.status()).toBe(200);
  const finished = (await done.json()) as { status: string; safety: string };
  expect(finished.status).toBe("completed");
  expect(finished.safety).toBe("signal");
  await asPatient.dispose();
  return core.session_id;
};

const openQueue = async (page: Page) => {
  await page.context().clearCookies();
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await expect(page.getByRole("button", { name: "Все" })).toBeVisible();
  await expect(page.getByText(/Очередь эскалаций/)).toBeVisible();
};

const findInQueue = async (page: Page, sessionId: string): Promise<QueueItem> => {
  const asStaff = await apiAs(page);
  const resp = await asStaff.get("/api/v1/escalations");
  expect(resp.status()).toBe(200);
  const body: QueueBody = await resp.json();
  await asStaff.dispose();
  const mine = body.items.find((i) => i.checkin_session_id === sessionId);
  expect(mine).toBeTruthy();
  return mine as QueueItem;
};

test("чужой пациент: врач видит ответы риска, карточка недоступна, сигнал закрывается", async ({ page }) => {
  await registerPatient(page, freshPatientEmail("queue"));
  await page.getByRole("button", { name: "Даю согласие" }).click();
  await expect(page.getByText("Согласие активно")).toBeVisible();
  const sessionId = await riskCheckin(page);

  await openQueue(page);
  const item = await findInQueue(page, sessionId);
  expect(item.in_roster).toBe(false);
  expect(item.risk_answers).toMatchObject({ HR4: 1, RS1: 0, RS2: 0, RS3: 0 });

  const row = page.locator(`tr[data-escalation="${item.id}"]`);
  await expect(row).toBeVisible();
  await expect(row).toContainText("HR4 Да");
  await expect(row).toContainText("RS1 Нет");
  await expect(row).toContainText("RS3 Нет");
  // Не roster-пациент: карточка отдала бы 403, поэтому ФИО — просто текст.
  await expect(row.locator("a")).toHaveCount(0);

  await row.getByRole("button", { name: /Закрыть сигнал/ }).click();
  await expect(row).toHaveCount(0);

  const asStaff = await apiAs(page);
  const resp = await asStaff.get("/api/v1/escalations");
  const body: QueueBody = await resp.json();
  await asStaff.dispose();
  expect(body.items.map((i) => i.id)).not.toContain(item.id);
});

test("ростерный пациент: имя в очереди ведёт в карточку пациента", async ({ page }) => {
  await registerPatient(page, freshPatientEmail("queue-roster"));
  await page.getByRole("button", { name: "Даю согласие" }).click();
  await expect(page.getByText("Согласие активно")).toBeVisible();
  const sessionId = await riskCheckin(page);

  // Закрепляем пациента за демо-врачом через админский API.
  const admin = await loginApi("admin@compass.demo");
  const doc = await loginApi("doc@compass.demo");
  const queueResp = await admin.ctx.get("/api/v1/escalations");
  expect(queueResp.status()).toBe(200);
  const queueBody: QueueBody = await queueResp.json();
  const patientId = queueBody.items.find((i) => i.checkin_session_id === sessionId)?.patient_id;
  expect(patientId).toBeTruthy();
  const grant = await admin.ctx.post("/api/v1/admin/roster", {
    data: { patient_id: patientId, clinician_id: doc.userId },
  });
  // 201 — закрепили; 409 — уже закреплён прошлым прогоном, тоже годится.
  expect([201, 409]).toContain(grant.status());

  await openQueue(page);
  const item = await findInQueue(page, sessionId);
  expect(item.in_roster).toBe(true);

  const row = page.locator(`tr[data-escalation="${item.id}"]`);
  const link = row.locator(`a[href="#patient=${item.patient_id}"]`);
  await expect(link).toBeVisible();
  await link.click();
  await expect(page).toHaveURL(new RegExp(`patient=${item.patient_id}`));
  await expect(page.getByRole("button", { name: "← Назад к списку" })).toBeVisible();
  await page.getByRole("button", { name: "← Назад к списку" }).click();

  // Убираем сигнал, чтобы очередь не засорялась прогонами; повторное закрытие — не ошибка.
  const resolved = await doc.ctx.post(`/api/v1/escalations/${item.id}/resolve`);
  expect([200, 409]).toContain(resolved.status());
  await admin.ctx.dispose();
  await doc.ctx.dispose();
});
