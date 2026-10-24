// Регистрация нежелательного явления (задача кейса 3): форма врача в отчёте пациента.
import { expect, test, type APIResponse } from "@playwright/test";
import { DEMO_PASSWORD, freshPatientEmail, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: APIResponse) => r.json();

test.describe("Нежелательные явления", () => {
  test("врач регистрирует НЯ в карточке пациента, оно появляется в таблице", async ({ page, request }) => {
    // API-подготовка: свежий врач и пациент, согласие, roster через админа.
    const docReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("aedoc"), password: DEMO_PASSWORD, role: "CLINICIAN" },
    }));
    const patReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("aepat"), password: DEMO_PASSWORD, role: "PATIENT" },
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

    // UI врача: открываем карточку пациента.
    await page.goto(`${BASE}/`);
    await page.getByLabel("Email").fill(docReg.email);
    await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
    await page.getByLabel("Код 2FA").fill(totp(docReg.totp_secret as string));
    await page.getByRole("button", { name: "Войти" }).click();

    const row = page.locator("tbody tr", { hasText: patReg.email }).first();
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "Открыть" }).click();
    await expect(page.getByRole("heading", { name: "Нежелательные явления", exact: true })).toBeVisible();

    // Форма одна и живёт внутри блока проверки: сначала раскрываем «Сообщить о НЯ».
    await page.getByRole("button", { name: "Сообщить о НЯ" }).click();
    // Пустое описание → сохранение неактивно.
    await expect(page.getByRole("button", { name: "Сохранить НЯ" })).toBeDisabled();

    // Заполняем НЯ: текст, тяжесть «тяжёлое», препарат, действие.
    const eventText = `Головокружение e2e ${Date.now()}`;
    await page.getByPlaceholder("Описание НЯ").fill(eventText);
    await page.getByLabel("Тяжесть НЯ").selectOption("severe");
    await page.getByPlaceholder("Препарат (напр. Авиандр)").fill("Авиандр");
    await page.getByLabel("Действие при НЯ").selectOption("referred");
    await page.getByRole("button", { name: "Сохранить НЯ" }).click();

    // НЯ появилось в таблице с бейджем тяжести RED (severe).
    const aeRow = page.locator("tbody tr", { hasText: eventText }).first();
    await expect(aeRow).toBeVisible();
    await expect(aeRow.locator(".badge.RED")).toBeVisible();
  });
});