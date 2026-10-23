// e2e экрана «Чаты» врача (FR-29): ассистент по базе знаний и переписка с пациентами
// в одном разделе; навигация «Пациенты · Чаты · Библиотека · Напоминания».
import { expect, test, type APIResponse } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, freshPatientEmail, login, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: APIResponse) => r.json();

test.describe("Экран чатов врача", () => {
  test("Чаты → Ассистент: диалог создаётся и отвечает по базе знаний", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);

    await page.getByRole("button", { name: "Чаты" }).click();
    await page.getByRole("tab", { name: "Ассистент" }).click();
    await expect(page.getByRole("heading", { name: "Ассистент врача" })).toBeVisible();

    await page.locator('.icon-btn[title="Новый диалог"]').click();
    await page.getByLabel("Вопрос").fill("Какой порог тяжести у шкалы PHQ-9?");
    await page.getByRole("button", { name: "Спросить" }).click();

    // В CI без LLM ответ может быть NO_KB_DATA — тогда ассистент честно говорит об этом.
    const answer = page.locator(".bubble", { hasText: "Ассистент" }).last();
    await expect(answer).toBeVisible({ timeout: 20_000 });
    await expect(answer).toContainText(/По базе знаний|NO_KB_DATA|не удалось/i);
  });

  test("Чаты → Пациенты: переписка видна, врач отвечает", async ({ page, request }) => {
    // API-подготовка: свежий врач, пациент с согласием, roster и сообщение пациента.
    const docReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("uxdoc"), password: DEMO_PASSWORD, role: "CLINICIAN" },
    }));
    const patReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("uxpat"), password: DEMO_PASSWORD, role: "PATIENT" },
    }));
    const patTok = (
      await j(await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: patReg.email, password: DEMO_PASSWORD },
      }))
    ).access_token as string;
    await request.post(`${BASE}/api/v1/consent`, {
      data: { version: 1 },
      headers: { Authorization: `Bearer ${patTok}` },
    });

    const admTok = (
      await j(await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: "admin@compass.demo", password: DEMO_PASSWORD, otp: totp("JBSWY3DPEHPK3PXP") },
      }))
    ).access_token as string;
    await request.post(`${BASE}/api/v1/admin/roster`, {
      data: { patient_id: patReg.id, clinician_id: docReg.id },
      headers: { Authorization: `Bearer ${admTok}` },
    });

    const incoming = `Здравствуйте, нужен совет ${Date.now()}`;
    await request.post(`${BASE}/api/v1/chat/thread`, {
      data: { content: incoming },
      headers: { Authorization: `Bearer ${patTok}` },
    });

    // UI: врач открывает «Чаты → Пациенты».
    await page.goto(`${BASE}/`);
    await page.getByLabel("Email").fill(docReg.email);
    await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
    await page.getByLabel("Код 2FA").fill(totp(docReg.totp_secret as string));
    await page.getByRole("button", { name: "Войти" }).click();

    await page.getByRole("button", { name: "Чаты" }).click();
    await page.getByRole("tab", { name: "Пациенты" }).click();
    await expect(
      page.getByRole("heading", { name: "Переписка с пациентами" }),
    ).toBeVisible();

    // Пациент есть в списке с превью последнего сообщения; открываем переписку.
    const item = page.locator(".chat-item", { hasText: patReg.email }).first();
    await expect(item).toBeVisible();
    await item.click();
    await expect(page.locator(".bubble", { hasText: incoming })).toBeVisible();

    const reply = `Здравствуйте! Расскажите подробнее ${Date.now()}`;
    await page.getByLabel("Сообщение пациенту").fill(reply);
    await page.getByRole("button", { name: "Отправить" }).click();
    await expect(page.locator(".bubble", { hasText: reply })).toBeVisible();
  });

  test("старые разделы врача доступны: Пациенты и Библиотека", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);

    await expect(page.getByRole("heading", { name: /Мои пациенты/ })).toBeVisible();
    await expect(page.getByText("Сначала красные")).toBeVisible();

    await page.getByRole("button", { name: "Библиотека" }).click();
    await page.getByRole("button", { name: "Пациенты", exact: true }).click();
    await expect(page.getByRole("heading", { name: /Мои пациенты/ })).toBeVisible();

    await page.getByRole("button", { name: /^Напоминания/ }).click();
    await expect(page.getByRole("heading", { name: /Напоминания/ })).toBeVisible();
  });
});
