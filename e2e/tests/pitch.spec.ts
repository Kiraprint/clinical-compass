// e2e питч-сценария (docs/04 §6): пациент → RED-скрининг → эскалация → чат HOTLINE → врач видит RED.
import { expect, test } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, login, openSection } from "./helpers";

test.describe("Питч-сценарий", () => {
  test("пациент: скрининг с суицидальным ответом → RED + контакты помощи", async ({ page }) => {
    await login(page, "p1@compass.demo", DEMO_PASSWORD);
    await expect(page.getByRole("button", { name: "Скрининг", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Скрининг", exact: true }).click();
    // Многошкальный режим (Option A): если врач назначил >1 шкалы, сначала выбираем первую.
    const chooser = page.getByText("Шкалы назначены вашим врачом — выберите одну:");
    if (await chooser.isVisible().catch(() => false)) {
      await page.locator(".card .row button").first().click();
    }
    await page.getByRole("button", { name: "Начать скрининг" }).click();

    const questions = page.locator(".qa");
    await expect(questions).toHaveCount(9);
    for (let i = 0; i < 9; i++) {
      const question = questions.nth(i);
      const text = await question.locator("p").innerText();
      const value = text.includes("умереть") ? 3 : 0;
      await question.locator("button").nth(value).click();
    }
    await page.getByRole("button", { name: "Завершить" }).click();

    await expect(page.locator(".badge.RED")).toBeVisible();
    await expect(page.getByText("Единый телефон доверия")).toBeVisible();
    await expect(page.getByText("112")).toBeVisible();
  });

  test("пациент: чат отвечает горячей линией при RED", async ({ page }) => {
    await login(page, "p2@compass.demo", DEMO_PASSWORD);
    await page.getByRole("button", { name: "Чат поддержки" }).click();
    await page.locator("input").fill("Мне очень плохо");
    await page.getByRole("button", { name: "Отправить" }).click();
    // Сообщения чата персистятся в БД между прогонами, поэтому берём последний ответ.
    await expect(page.getByText("Мы заметили, что вам сейчас тяжело").last()).toBeVisible();
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
