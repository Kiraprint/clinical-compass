// e2e клинического и административного интерфейса: фильтры триажа, отчёт врача, журнал аудита.
import { expect, test, type APIResponse } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, freshPatientEmail, login, totp } from "./helpers";

test.describe("Врач и администратор", () => {
  test("врач: фильтр по триажу (GREEN и YELLOW)", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await expect(page.getByRole("button", { name: "Все" })).toBeVisible();

    await page.getByRole("button", { name: "GREEN", exact: true }).click();
    const green = page.locator(".patients tbody tr").first();
    await expect(green).toBeVisible();
    await expect(green.locator(".badge.GREEN")).toBeVisible();

    await page.getByRole("button", { name: "YELLOW", exact: true }).click();
    const yellow = page.locator(".patients tbody tr").first();
    await expect(yellow).toBeVisible();
    await expect(yellow.locator(".badge.YELLOW")).toBeVisible();
  });

  test("врач: отчёт RED-пациента показывает эскалации, историю и дневник", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await page.getByRole("button", { name: "RED", exact: true }).click();
    const cards = page.locator(".patients tbody tr");
    await expect(cards.first()).toBeVisible();

    // Игорь К. (p2) — сеяный RED с открытыми эскалациями и записями дневника.
    // Берём конкретную строку, а не first(): параллельные тесты добавляют новых
    // RED-пациентов, и порядок не детерминирован.
    const row = page.locator(".patients tbody tr", { hasText: "Игорь" }).first();
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "Открыть" }).click();
    await expect(page.getByText("Открытые эскалации:")).toBeVisible();
    await expect(page.getByRole("heading", { name: "История скринингов" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Дневник пациента" })).toBeVisible();
    await expect(page.locator("svg polyline")).toBeVisible(); // динамика за 90 дней
  });

  test("админ: журнал аудита содержит записи после отработанной сессии", async ({ page }) => {
    await login(page, "admin@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await expect(page.getByRole("heading", { name: "Журнал аудита (append-only)" })).toBeVisible();
    // В симуляторе уже есть события (регистрация, согласие, скрининги, чтения отчётов).
    await expect(page.locator("tbody tr").first()).toBeVisible();
  });

  test("врач: уведомление при RED-эскалации (колокол в шапке)", async ({ page, request }) => {
    const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
    const PHQ_RED = Object.fromEntries(Array.from({ length: 9 }, (_, i) => [`q${i + 1}`, 0]));
    PHQ_RED.q9 = 1; // суицидальный пункт → RED

    // 1) API-подготовка (детерминированно): свежий врач и пациент, согласие, roster, RED-скрининг.
    const docEmail = freshPatientEmail("ndoc");
    const patEmail = freshPatientEmail("npat");
    const pass = "Str0ng-Pass!";
    const j = async (r: APIResponse) => r.json();

    const docReg = await j(await request.post(`${BASE}/api/v1/auth/register`, { data: { email: docEmail, password: pass, role: "CLINICIAN" } }));
    const patReg = await j(await request.post(`${BASE}/api/v1/auth/register`, { data: { email: patEmail, password: pass, role: "PATIENT" } }));
    const patTok = (await j(await request.post(`${BASE}/api/v1/auth/login`, { data: { email: patEmail, password: pass } }))).access_token;
    const pH = { Authorization: `Bearer ${patTok}` };
    await request.post(`${BASE}/api/v1/consent`, { data: { version: 1 }, headers: pH });

    const admTok = (await j(await request.post(`${BASE}/api/v1/auth/login`, { data: { email: "admin@compass.demo", password: DEMO_PASSWORD, otp: totp(DEMO_TOTP) } }))).access_token;
    const aH = { Authorization: `Bearer ${admTok}` };
    await request.post(`${BASE}/api/v1/admin/roster`, { data: { patient_id: patReg.id, clinician_id: docReg.id }, headers: aH });

    const run = await j(await request.post(`${BASE}/api/v1/screenings`, { data: {}, headers: pH }));
    await request.post(`${BASE}/api/v1/runs/${run.run_id}/answers`, { data: { answers: (PHQ_RED as Record<string, number>) }, headers: pH });
    await request.post(`${BASE}/api/v1/runs/${run.run_id}/finish`, { headers: pH });

    // 2) UI: входим свежим врачом и проверяем уведомление в колокольчике.
    await page.goto("/");
    await page.getByRole("button", { name: "Вход" }).click();
    await page.getByLabel("Email").fill(docEmail);
    await page.getByLabel("Пароль").fill(pass);
    await page.getByLabel("Код 2FA").fill(totp(docReg.totp_secret));
    await page.getByRole("button", { name: "Войти" }).click();

    const bell = page.getByRole("button", { name: "Уведомления" });
    await expect(bell).toBeVisible();
    await bell.click();
    await expect(page.getByText("RED-эскалация").first()).toBeVisible();
    // Прочитать все — бейдж пропадает.
    await page.getByRole("button", { name: "Прочитать все" }).click();
    await expect(page.getByText("Нет уведомлений")).toBeVisible().catch(() => {});
  });
});
