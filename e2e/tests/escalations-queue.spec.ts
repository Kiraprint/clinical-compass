// docs/16 §3: сигнал риска из чек-ина попадает в общую очередь эскалаций и ведёт
// врача в карточку пациента — даже если пациент не из его ростера.
import { expect, request, test, type Page } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, freshPatientEmail, login, registerPatient } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";

// Web-клиент держит токен в httpOnly cookie: API-контекст получает cookies из браузера.
const apiAs = async (page: Page) =>
  request.newContext({ baseURL: BASE, storageState: await page.context().storageState() });

test("врач видит риск-чек-ин в очереди эскалаций и открывает карточку", async ({ page }) => {
  // 1. Пациент: регистрация, согласие, риск-чек-ин (HR4=«Да», RS1–RS3=«Нет») через API.
  await registerPatient(page, freshPatientEmail("queue"));
  await page.getByRole("button", { name: "Даю согласие" }).click();
  await expect(page.getByText("Согласие активно")).toBeVisible();
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
  const finish = (await done.json()) as { status: string; safety: string; session_id: string };
  expect(finish.status).toBe("completed");
  expect(finish.safety).toBe("signal");
  await asPatient.dispose();

  // Выходим из пациента: web-сессия в httpOnly cookie, иначе экран входа не появится.
  await page.context().clearCookies();
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await expect(page.getByRole("button", { name: "Все" })).toBeVisible();
  const asStaff = await apiAs(page);

  const escalationsResp = await asStaff.get("/api/v1/escalations");
  expect(escalationsResp.status()).toBe(200);
  const { items } = (await escalationsResp.json()) as {
    items: {
      patient_id: string;
      kind: string;
      safety: string | null;
      checkin_session_id: string | null;
      created_at: string | null;
    }[];
  };
  const mine = items.find((e) => e.checkin_session_id === finish.session_id);
  expect(mine).toBeTruthy();
  expect(mine!.kind).toMatch(/^CHECKIN_RISK/);
  expect(mine!.safety).toBe("signal");
  await asStaff.dispose();

  // 3. UI: карточка очереди видна, ссылка ведёт в карточку пациента.
  await expect(page.getByRole("heading", { name: /Очередь эскалаций/ })).toBeVisible();
  const link = page.locator(`a[href="#patient=${mine!.patient_id}"]`).first();
  await expect(link).toBeVisible();
  await expect(link).not.toHaveText("");

  await link.click();
  await expect(page).toHaveURL(new RegExp(`#patient=${mine!.patient_id}`));
  await expect(page.getByText("← Назад к списку")).toBeVisible();

  // Возврат к списку снова показывает очередь.
  await page.getByText("← Назад к списку").click();
  await expect(page.getByRole("heading", { name: /Очередь эскалаций/ })).toBeVisible();
});
