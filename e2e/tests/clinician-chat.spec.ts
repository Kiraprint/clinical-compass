// Чат врач ↔ пациент (задача кейса 6): сообщение врача видно пациенту, ответ — врачу.
import { expect, test, type APIResponse } from "@playwright/test";
import { DEMO_PASSWORD, freshPatientEmail, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: APIResponse) => r.json();

test.describe("Чат врач ↔ пациент", () => {
  test("врач пишет пациенту, пациент отвечает — обе стороны видят переписку", async ({ page, browser, request }) => {
    // API-подготовка: свежие врач и пациент, согласие, roster.
    const docReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("chdoc"), password: DEMO_PASSWORD, role: "CLINICIAN" },
    }));
    const patReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("chpat"), password: DEMO_PASSWORD, role: "PATIENT" },
    }));
    const patTok = (
      await j(await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: patReg.email, password: DEMO_PASSWORD },
      }))
    ).access_token as string;
    await request.post(`${BASE}/api/v1/consent`, { data: { version: 1 }, headers: { Authorization: `Bearer ${patTok}` } });

    const admTok = (
      await j(await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: "admin@compass.demo", password: DEMO_PASSWORD, otp: totp("JBSWY3DPEHPK3PXP") },
      }))
    ).access_token as string;
    await request.post(`${BASE}/api/v1/admin/roster`, {
      data: { patient_id: patReg.id, clinician_id: docReg.id },
      headers: { Authorization: `Bearer ${admTok}` },
    });

    // Врач открывает карточку пациента и пишет сообщение.
    await page.goto(`${BASE}/`);
    await page.getByLabel("Email").fill(docReg.email);
    await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
    await page.getByLabel("Код 2FA").fill(totp(docReg.totp_secret as string));
    await page.getByRole("button", { name: "Войти" }).click();

    const row = page.locator("tbody tr", { hasText: patReg.email }).first();
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "Открыть" }).click();

    // FR-29: отправка — на отдельном экране «Чаты»; карточка показывает превью.
    await page.getByRole("button", { name: "Открыть в чатах" }).click();
    await expect(
      page.getByRole("heading", { name: "Переписка с пациентами" }),
    ).toBeVisible();

    const docMsg = `Привет, как вы себя чувствуете? ${Date.now()}`;
    await page.getByLabel("Сообщение пациенту").fill(docMsg);
    await page.getByRole("button", { name: "Отправить" }).click();
    await expect(page.locator(".bubble", { hasText: docMsg })).toBeVisible();

    // Пациент (отдельный контекст) видит сообщение врача и отвечает.
    const ctx2 = await browser.newContext();
    const page2 = await ctx2.newPage();
    await page2.goto(`${BASE}/`);
    await page2.getByLabel("Email").fill(patReg.email);
    await page2.getByLabel("Пароль").fill(DEMO_PASSWORD);
    await page2.getByRole("button", { name: "Войти" }).click();
    await page2.getByRole("button", { name: "Чат поддержки" }).click();
    await expect(page2.locator(".msg", { hasText: docMsg })).toBeVisible();

    const patMsg = `Спасибо, лучше! ${Date.now()}`;
    await page2.locator("input").fill(patMsg);
    await page2.getByRole("button", { name: "Отправить" }).click();
    await expect(page2.locator(".msg", { hasText: patMsg })).toBeVisible();

    // Врач перезагружает страницу — hash #chats=patient:<id> возвращает в диалог.
    await page.reload();
    await expect(page.locator(".bubble", { hasText: patMsg })).toBeVisible();
    await ctx2.close();
  });
});