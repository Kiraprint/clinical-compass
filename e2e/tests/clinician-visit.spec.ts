// Запись приёма (задача кейса 6, п. «приём»): врач фиксирует беседу с пациентом,
// запись появляется в карточке. Заметка — спецкатегория ПДн и уничтожается
// при отзыве согласия (проверяется в test_consent.py).
import { expect, test, type APIResponse } from "@playwright/test";
import { DEMO_PASSWORD, freshPatientEmail, openSection, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: APIResponse) => r.json();

test.describe("Приёмы врача", () => {
  test("врач сохраняет запись приёма, она видна в карточке пациента", async ({ page, request }) => {
    const docReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("vsdoc"), password: DEMO_PASSWORD, role: "CLINICIAN" },
    }));
    const patReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("vspat"), password: DEMO_PASSWORD, role: "PATIENT" },
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

    await page.goto(`${BASE}/`);
    await page.getByLabel("Email").fill(docReg.email);
    await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
    await page.getByLabel("Код 2FA").fill(totp(docReg.totp_secret as string));
    await page.getByRole("button", { name: "Войти" }).click();

    await openSection(page, "Пациенты");

    const row = page.locator(".patients tbody tr", { hasText: patReg.email }).first();
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "Открыть" }).click();

    await expect(page.getByRole("heading", { name: /Приёмы/ })).toBeVisible();
    const notes = `Обсудили сон и тревогу, план — продолжить терапию. ${Date.now()}`;
    await page.getByLabel("Тип приёма").selectOption("online");
    await page.getByPlaceholder("Длительность, мин").fill("50");
    await page.getByPlaceholder("Тема беседы, результаты, план, договорённости…").fill(notes);
    await page.getByRole("button", { name: "Сохранить приём" }).click();

    const saved = page.locator("tbody tr", { hasText: notes }).first();
    await expect(saved).toBeVisible();
    await expect(saved).toContainText("Онлайн");
    await expect(saved).toContainText("50");

    // Запись переживает перезагрузку карточки (сохранена на сервере).
    await page.reload();
    await expect(page.locator("tbody tr", { hasText: notes }).first()).toBeVisible();
  });
});
