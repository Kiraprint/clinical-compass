// Врачебная шкала (HDRS-17): врач заполняет в карточке, отчёт сразу показывает
// новый риск. Шкала недоступна пациенту (audience=CLINICIAN).
import { expect, test, type APIResponse } from "@playwright/test";
import { DEMO_PASSWORD, freshPatientEmail, openSection, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: APIResponse) => r.json();

test.describe("Врачебная оценка (HDRS-17)", () => {
  test("врач заполняет клиницистскую шкалу → отчёт показывает RED-эскалацию", async ({ page, request }) => {
    // API-подготовка: свежий врач и пациент, согласие, roster через админа.
    const docReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("asdoc"), password: DEMO_PASSWORD, role: "CLINICIAN" },
    }));
    const patReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("aspat"), password: DEMO_PASSWORD, role: "PATIENT" },
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

    // UI врача.
    await page.goto(`${BASE}/`);
    await page.getByLabel("Email").fill(docReg.email);
    await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
    await page.getByLabel("Код 2FA").fill(totp(docReg.totp_secret as string));
    await page.getByRole("button", { name: "Войти" }).click();
    await openSection(page, "Пациенты");

    const row = page.locator(".patients tbody tr", { hasText: patReg.email }).first();
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "Открыть" }).click();

    // Форма врачебной оценки теперь внутри блока «Назначить скрининг»
    // (по умолчанию раскрыт): шкала HDRS-17, пункт h3 = 2 (суицидальные мысли).
    await expect(page.locator("summary", { hasText: "Назначить скрининг" })).toBeVisible();
    await expect(page.getByLabel("Шкала врачебной оценки")).toHaveValue("HDRS-17");
    await page.getByLabel("Суицидальные мысли").selectOption("2");
    await page.getByRole("button", { name: "Сохранить оценку" }).click();

    // Итог сразу в форме, а карточка перезагружается — появляется алёрт риска.
    await expect(page.getByText(/· RED/).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText("Высокий риск: требуется разбор.")).toBeVisible({ timeout: 15000 });

    // Пациенту такая шкала недоступна: каталог её не содержит.
    const cat = await request.get(`${BASE}/api/v1/questionnaires`, {
      headers: { Authorization: `Bearer ${patTok}` },
    });
    const codes = (await j(cat)).map((s: { code: string }) => s.code);
    expect(codes).toContain("PSS-10");
    expect(codes).not.toContain("HDRS-17");
  });
});
