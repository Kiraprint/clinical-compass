// Многошкальный скрининг (item 1, «Option A»): врач назначает 2 шкалы,
// пациент выбирает одну, проходит её и видит результат с кодом выбранной шкалы.
import { expect, test, type APIResponse } from "@playwright/test";
import { DEMO_PASSWORD, freshPatientEmail, openScreening, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: APIResponse) => r.json();

test.describe("Многошкальный скрининг", () => {
  test("пациент выбирает GAD-7 из двух назначенных и завершает скрининг", async ({ page, request }) => {
    // API-подготовка: свежие врач и пациент (демо разрешает регистрацию CLINICIAN),
    // согласие пациента, roster через админа, назначение двух шкал врачом.
    const docReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("msdoc"), password: DEMO_PASSWORD, role: "CLINICIAN" },
    }));
    const docTok = (
      await j(await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: docReg.email, password: DEMO_PASSWORD, otp: totp(docReg.totp_secret as string) },
      }))
    ).access_token as string;
    const dH = { Authorization: `Bearer ${docTok}` };

    const patReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("mspat"), password: DEMO_PASSWORD, role: "PATIENT" },
    }));
    const patTok = (
      await j(await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: patReg.email, password: DEMO_PASSWORD },
      }))
    ).access_token as string;
    const pH = { Authorization: `Bearer ${patTok}` };
    await request.post(`${BASE}/api/v1/consent`, { data: { version: 1 }, headers: pH });

    const admTok = (
      await j(await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: "admin@compass.demo", password: DEMO_PASSWORD, otp: totp("JBSWY3DPEHPK3PXP") },
      }))
    ).access_token as string;
    await request.post(`${BASE}/api/v1/admin/roster`, {
      data: { patient_id: patReg.id, clinician_id: docReg.id },
      headers: { Authorization: `Bearer ${admTok}` },
    });

    // Врач назначает две шкалы.
    const prescribe = await request.put(
      `${BASE}/api/v1/clinician/patients/${patReg.id}/prescribed-scales`,
      { data: { scales: ["PHQ-9", "GAD-7"] }, headers: dH }
    );
    expect(prescribe.status()).toBe(200);

    // UI пациента: селекта нет, предлагается выбор из назначенных шкал.
    await page.goto(`${BASE}/`);
    await page.getByLabel("Email").fill(patReg.email);
    await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
    await page.getByRole("button", { name: "Войти" }).click();
    await openScreening(page);
    await expect(page.getByText("Шкалы назначены вашим врачом — выберите одну:")).toBeVisible();
    await expect(page.locator("select")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Начать скрининг" })).toBeDisabled();

    // Выбираем GAD-7, стартуем, отвечаем 0 на все пункты (7 вопросов).
    await page.getByRole("button", { name: "Шкала генерализованной тревоги GAD-7" }).click();
    await page.getByRole("button", { name: "Начать скрининг" }).click();
    const questions = page.locator(".qa");
    await expect(questions).toHaveCount(7);
    for (let i = 0; i < 7; i++) {
      // Варианты подписаны якорями шкалы, а не голыми числами.
      await questions.nth(i).getByRole("button", { name: "Совсем нет" }).click();
    }
    await page.getByRole("button", { name: "Завершить" }).click();

    // Результат выбранной шкалы, а не дефолтной.
    await expect(page.getByRole("heading", { name: "Результат скрининга — GAD-7" })).toBeVisible();
    await expect(page.locator(".badge.GREEN")).toBeVisible();
    await expect(page.getByText("Балл 0 · NONE")).toBeVisible();
  });
});