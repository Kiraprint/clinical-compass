// e2e питч-сценария (docs/04 §6): пациент → RED-скрининг → эскалация → чат HOTLINE → врач видит RED.
import { expect, test } from "@playwright/test";
import {
  DEMO_PASSWORD,
  DEMO_TOTP,
  freshPatientEmail,
  login,
  openPatientChat,
  openScreening,
  openSection,
} from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";

test.describe("Питч-сценарий", () => {
  test("пациент: скрининг по 8 пунктам → RED + контакты помощи", async ({ page }) => {
    await login(page, "p1@compass.demo", DEMO_PASSWORD);
    await openScreening(page);
    // Многошкальный режим (Option A): если врач назначил >1 шкалы, сначала выбираем первую.
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

  test("пациент: чат отвечает горячей линией один раз в сутки", async ({ page, request }) => {
    // Свежий пациент: у демо-аккаунтов лимит автоответа израсходован прошлыми прогонами.
    const reg = await (
      await request.post(`${BASE}/api/v1/auth/register`, {
        data: { email: freshPatientEmail("pitchchat"), password: DEMO_PASSWORD, role: "PATIENT" },
      })
    ).json();
    const loginResp = await (
      await request.post(`${BASE}/api/v1/auth/login`, {
        data: { email: reg.email, password: DEMO_PASSWORD },
      })
    ).json();
    const h = { Authorization: `Bearer ${loginResp.access_token}` };
    await request.post(`${BASE}/api/v1/consent`, { data: { version: 1 }, headers: h });

    // RED-скрининг через API: 8 пунктов PHQ-9 (без q9) по 3 балла.
    const run = await (
      await request.post(`${BASE}/api/v1/screenings`, { data: {}, headers: h })
    ).json();
    const answers = Object.fromEntries([1, 2, 3, 4, 5, 6, 7, 8].map((i) => [`q${i}`, 3]));
    await request.post(`${BASE}/api/v1/runs/${run.run_id}/answers`, { data: { answers }, headers: h });
    await request.post(`${BASE}/api/v1/runs/${run.run_id}/finish`, { headers: h });

    await page.goto(`${BASE}/`);
    await page.getByLabel("Email").fill(reg.email);
    await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
    await page.getByRole("button", { name: "Войти" }).click();
    await openPatientChat(page);

    await page.getByLabel("Сообщение врачу").fill("Мне очень плохо");
    await page.getByRole("button", { name: "Отправить" }).click();
    await expect(
      page.getByText("Если станет тяжелее, позвоните по одному из номеров помощи ниже.").last()
    ).toBeVisible();

    // Второе сообщение в тот же день сохраняется, но автоответа уже нет (FR-34).
    const systems = page.locator(".msg.system");
    const before = await systems.count();
    await page.getByLabel("Сообщение врачу").fill("И ещё немного");
    await page.getByRole("button", { name: "Отправить" }).click();
    await expect(page.getByText("Помощник отвечает не чаще одного раза в день")).toBeVisible();
    await expect(systems).toHaveCount(before);
  });

  test("врач: входит с 2FA и видит RED-пациентов", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await openSection(page, "Пациенты");
    await expect(page.getByRole("button", { name: "Все" })).toBeVisible();

    await page.getByRole("button", { name: "RED", exact: true }).click();
    const cards = page.locator(".patients tbody tr");
    await expect(cards.first()).toBeVisible();
    await expect(cards.first().locator(".badge.RED")).toBeVisible();

    await cards.first().getByRole("button", { name: "Открыть" }).click();
    await expect(page.getByText("Риск по последней оценке:")).toBeVisible();
  });
});
