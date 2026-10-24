// Негативные e2e RBAC/IDOR: роли не читают чужие ресурсы, ростер-гейт работает.
// Проверки на уровне API (request) + изоляция ролей в UI.
import { expect, test, type APIResponse } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, freshPatientEmail, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: APIResponse) => r.json();

test.describe("RBAC и IDOR: негативные проверки", () => {
  test("API: без токена и пациент получают 401/403 на клинические и админ-ресурсы", async ({ request }) => {
    // Без токена — 401.
    expect((await request.get(`${BASE}/api/v1/clinician/patients`)).status()).toBe(401);
    expect((await request.get(`${BASE}/api/v1/admin/audit`)).status()).toBe(401);

    // Свежий пациент: RBAC-гейты require_clinician/require_admin → 403 ROLE_MISMATCH.
    const patEmail = freshPatientEmail("rbac");
    await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: patEmail, password: DEMO_PASSWORD, role: "PATIENT" },
    });
    const patTok = (
      await j(await request.post(`${BASE}/api/v1/auth/login`, { data: { email: patEmail, password: DEMO_PASSWORD } }))
    ).access_token as string;
    const pH = { Authorization: `Bearer ${patTok}` };
    expect((await request.get(`${BASE}/api/v1/clinician/patients`, { headers: pH })).status()).toBe(403);
    expect((await request.get(`${BASE}/api/v1/admin/audit`, { headers: pH })).status()).toBe(403);
    expect((await request.get(`${BASE}/api/v1/clinician/patients/00000000-0000-0000-0000-000000000000/report`, { headers: pH })).status()).toBe(403);
  });

  test("API: врач видит только пациентов из своего ростера, чужой пациент → 403 NO_ROSTER", async ({ request }) => {
    // Свежий врач (CLINICIAN регистрируется в демо-режиме) — TOTP приходит в ответе.
    const docEmail = freshPatientEmail("rdoc");
    const docReg = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: docEmail, password: DEMO_PASSWORD, role: "CLINICIAN" },
    }));
    const docTok = (
      await j(await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: docEmail, password: DEMO_PASSWORD, otp: totp(docReg.totp_secret as string) },
      }))
    ).access_token as string;
    const dH = { Authorization: `Bearer ${docTok}` };

    // Пациент A — свой (ростер через админа), пациент B — чужой (без ростера).
    const patA = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("rpa"), password: DEMO_PASSWORD, role: "PATIENT" },
    }));
    const patB = await j(await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("rpb"), password: DEMO_PASSWORD, role: "PATIENT" },
    }));

    const admTok = (
      await j(await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: "admin@compass.demo", password: DEMO_PASSWORD, otp: totp(DEMO_TOTP) },
      }))
    ).access_token as string;
    const aH = { Authorization: `Bearer ${admTok}` };
    await request.post(`${BASE}/api/v1/admin/roster`, {
      data: { patient_id: patA.id, clinician_id: docReg.id },
      headers: aH,
    });

    // Свой пациент в списке и в отчёте; чужой — 403 NO_ROSTER.
    const list = await j(await request.get(`${BASE}/api/v1/clinician/patients`, { headers: dH }));
    const ids = (list.items as { patient_id: string }[]).map((c) => c.patient_id);
    expect(ids).toContain(patA.id);
    expect(ids).not.toContain(patB.id);
    expect((await request.get(`${BASE}/api/v1/clinician/patients/${patA.id}/report`, { headers: dH })).status()).toBe(200);
    expect((await request.get(`${BASE}/api/v1/clinician/patients/${patB.id}/report`, { headers: dH })).status()).toBe(403);
  });

  test("UI: пациент видит только свои вкладки, без «Пациенты» и журнала аудита", async ({ page, request }) => {
    const email = freshPatientEmail("rpub");
    await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email, password: DEMO_PASSWORD, role: "PATIENT" },
    });
    const tok = (await j(await request.post(`${BASE}/api/v1/auth/login`, { data: { email, password: DEMO_PASSWORD } }))).access_token as string;
    await request.post(`${BASE}/api/v1/consent`, { data: { version: 1 }, headers: { Authorization: `Bearer ${tok}` } });

    await page.goto(`${BASE}/`);
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
    await page.getByRole("button", { name: "Войти" }).click();

    await expect(page.getByText("Скрининг, назначенный врачом")).toBeVisible();
    // Клинические/админ-элементы недоступны пациенту в UI.
    await expect(page.getByRole("button", { name: "Пациенты" })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Журнал аудита (append-only)" })).toHaveCount(0);
  });
});