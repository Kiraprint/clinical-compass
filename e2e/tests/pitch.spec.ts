// e2e питч-сценария (docs/04 §6): пациент → RED-скрининг → контакты помощи → чат.
import { expect, test, type APIRequestContext, type APIResponse } from "@playwright/test";
import {
  DEMO_PASSWORD,
  DEMO_TOTP,
  freshPatientEmail,
  login,
  openPatientChat,
  openSection,
  openScreening,
  totp,
} from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: APIResponse) => r.json();

/** Свежий пациент с назначенной врачом шкалой (баннер «Скрининг, назначенный врачом»). */
async function preparedPatient(request: APIRequestContext, scales: string[] = ["PHQ-9"]) {
  const docReg = await j(
    await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("pitchdoc"), password: DEMO_PASSWORD, role: "CLINICIAN" },
    })
  );
  const docTok = (
    await j(
      await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: docReg.email, password: DEMO_PASSWORD, otp: totp(docReg.totp_secret as string) },
      })
    )
  ).access_token as string;
  const patReg = await j(
    await request.post(`${BASE}/api/v1/auth/register`, {
      data: { email: freshPatientEmail("pitchpat"), password: DEMO_PASSWORD, role: "PATIENT" },
    })
  );
  const patTok = (
    await j(
      await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: patReg.email, password: DEMO_PASSWORD },
      })
    )
  ).access_token as string;
  const pH = { Authorization: `Bearer ${patTok}` };
  await request.post(`${BASE}/api/v1/consent`, { data: { version: 1 }, headers: pH });
  const admTok = (
    await j(
      await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: "admin@compass.demo", password: DEMO_PASSWORD, otp: totp("JBSWY3DPEHPK3PXP") },
      })
    )
  ).access_token as string;
  await request.post(`${BASE}/api/v1/admin/roster`, {
    data: { patient_id: patReg.id, clinician_id: docReg.id },
    headers: { Authorization: `Bearer ${admTok}` },
  });
  const prescribe = await request.put(
    `${BASE}/api/v1/clinician/patients/${patReg.id}/prescribed-scales`,
    { data: { scales }, headers: { Authorization: `Bearer ${docTok}` } }
  );
  expect(prescribe.status()).toBe(200);
  return { email: patReg.email as string, token: patTok };
}

/** RED-скрининг через API: 8 пунктов PHQ-9 (без q9) по 3 балла. */
async function redScreening(request: APIRequestContext, token: string) {
  const h = { Authorization: `Bearer ${token}` };
  const run = await j(await request.post(`${BASE}/api/v1/screenings`, { data: {}, headers: h }));
  const answers = Object.fromEntries([1, 2, 3, 4, 5, 6, 7, 8].map((i) => [`q${i}`, 3]));
  await request.post(`${BASE}/api/v1/runs/${run.run_id}/answers`, { data: { answers }, headers: h });
  await request.post(`${BASE}/api/v1/runs/${run.run_id}/finish`, { headers: h });
}

async function loginAs(page: import("@playwright/test").Page, email: string) {
  await page.goto(`${BASE}/`);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
  await page.getByRole("button", { name: "Войти" }).click();
}

test.describe("Питч-сценарий", () => {
  test("пациент: скрининг по 8 пунктам → RED + контакты помощи", async ({ page, request }) => {
    const { email } = await preparedPatient(request, ["PHQ-9"]);
    await loginAs(page, email);
    await openScreening(page);
    // Многошкальный режим (Option A): если назначено >1 шкалы, выбираем первую.
    const chooser = page.getByText("Шкалы назначены вашим врачом — выберите одну:");
    if (await chooser.isVisible().catch(() => false)) {
      await page.locator(".card .row button").first().click();
    }
    await page.getByRole("button", { name: "Начать скрининг" }).click();

    // FR-34: пункт 9 пациенту не задаётся — 8 пунктов, все по 3 балла → 24 ≥ порога RED.
    const questions = page.locator(".qa");
    await expect(questions).toHaveCount(8);
    for (let i = 0; i < 8; i++) {
      await questions.nth(i).locator("button").nth(3).click();
    }
    await page.getByRole("button", { name: "Завершить" }).click();

    await expect(page.locator(".badge.RED")).toBeVisible();
    await expect(page.getByText("Пункт 9 не задавался")).toBeVisible();
    await expect(page.getByText("Единый телефон доверия")).toBeVisible();
    await expect(page.getByText("112")).toBeVisible();
  });

  test("пациент: при RED помощник присылает номера помощи и после исчерпания лимита", async ({
    page,
    request,
  }) => {
    const { email, token } = await preparedPatient(request);
    await redScreening(request, token);
    await loginAs(page, email);
    await openPatientChat(page);

    const before = await page.locator(".msg.system").count();
    await page.getByLabel("Сообщение врачу").fill("Мне очень плохо");
    await page.getByRole("button", { name: "Отправить" }).click();
    await expect(
      page.getByText("Если станет тяжелее, позвоните по одному из номеров помощи ниже.").last()
    ).toBeVisible();

    // Второе сообщение в тот же день: лимит автоответа исчерпан, но кризисный
    // ответ не подавляется — номера помощи приходят и во второй раз.
    await page.getByLabel("Сообщение врачу").fill("И ещё немного");
    await page.getByRole("button", { name: "Отправить" }).click();
    await expect(page.locator(".alert-danger").filter({ hasText: "8-800-2000-122" })).toBeVisible();
    expect(await page.locator(".msg.system").count()).toBeGreaterThan(before);
  });

  test("пациент: без RED помощник отвечает один раз в сутки", async ({ page, request }) => {
    const { email } = await preparedPatient(request);
    await loginAs(page, email);
    await openPatientChat(page);

    await page.getByLabel("Сообщение врачу").fill("Как вести дневник?");
    await page.getByRole("button", { name: "Отправить" }).click();
    await expect(page.locator(".msg.system").first()).toBeVisible();
    const after = await page.locator(".msg.system").count();

    await page.getByLabel("Сообщение врачу").fill("И ещё вопрос");
    await page.getByRole("button", { name: "Отправить" }).click();
    await expect(page.getByText(/Помощник отвечает не чаще одного раза в день/)).toBeVisible();
    expect(await page.locator(".msg.system").count()).toBe(after);
  });

  test("врач: входит с 2FA и видит RED-пациентов", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    // FR-32: разделы врача живут в меню-«три полоски».
    await openSection(page, "Пациенты");
    await expect(page.locator(".badge.RED").first()).toBeVisible();
  });
});
