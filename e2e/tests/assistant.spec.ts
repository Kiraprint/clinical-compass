// e2e вкладки «Ассистент» врача: треды с контекстом, гибридный поиск по базе
// знаний + цитаты (FR-21). UI: колонка диалогов слева, пациенты треда — чипами.
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, login } from "./helpers";

/** Открывает «Чаты → Ассистент» и заводит новый диалог (он выбирается сразу). */
async function openNewThread(page: Page) {
  await page.getByRole("button", { name: "Чаты" }).click();
  await page.getByRole("tab", { name: "Ассистент" }).click();
  await expect(page.getByRole("heading", { name: "Ассистент врача" })).toBeVisible();
  await page.getByRole("button", { name: "Новый диалог" }).click();
  await expect(page.getByLabel("Вопрос")).toBeVisible();
}

test.describe("ИИ-ассистент врача", () => {
  test("тред отвечает с источниками по базе знаний", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await openNewThread(page);

    // Справочный запрос → ответ с цитатами.
    await page.getByLabel("Вопрос").fill("Какой порог тяжести у шкалы PHQ-9?");
    await page.getByRole("button", { name: "Спросить" }).click();
    await expect(page.getByText("По базе знаний найдено следующее:")).toBeVisible();
    await expect(
      page.locator(".assistant-sources").getByText("Источники:", { exact: true }),
    ).toBeVisible();

    // Запрос на диагноз в том же треде → отказ (красная линия).
    await page.getByLabel("Вопрос").fill("Поставь диагноз депрессии");
    await page.getByRole("button", { name: "Спросить" }).click();
    await expect(page.getByText("Я не могу поставить диагноз")).toBeVisible();
  });

  test("справка по препарату Авиандр — не red line, ответ с цитатой инструкции", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await openNewThread(page);

    await page.getByLabel("Вопрос").fill("Какая дозировка Авиандра?");
    await page.getByRole("button", { name: "Спросить" }).click();
    await expect(page.getByText("По базе знаний найдено следующее:")).toBeVisible({ timeout: 15000 });
    // Цитата чанка препарата (АВИАНДР).
    await expect(page.locator(".assistant-sources").getByText(/АВИАНДР/i).first()).toBeVisible();
  });

  test("пациент в контексте треда подмешивает карту в ответ", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await openNewThread(page);

    // Чип пациента из ротстера врача → карта попадает в контекст треда.
    await page.getByTitle("Добавить в контекст").first().click();
    await page.getByLabel("Вопрос").fill("Какая динамика пациента по шкале PHQ-9?");
    await page.getByRole("button", { name: "Спросить" }).click();
    await expect(
      page.locator(".assistant-sources").getByText(/Карта пациента/),
    ).toBeVisible({ timeout: 15000 });
  });
});
