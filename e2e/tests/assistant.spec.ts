// e2e вкладки «Ассистент» врача: гибридный поиск по базе знаний + цитаты.
import { expect, test } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, login } from "./helpers";

test.describe("ИИ-ассистент врача", () => {
  test("вкладка «Ассистент» отвечает с источниками по базе знаний", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);

    // Переключаемся на вкладку ассистента.
    await page.getByRole("button", { name: "Ассистент" }).click();
    await expect(page.getByRole("heading", { name: "Ассистент врача" })).toBeVisible();

    // Справочный запрос → ответ с цитатами.
    await page.getByLabel("Вопрос").fill("Какой порог тяжести у шкалы PHQ-9?");
    await page.getByRole("button", { name: "Спросить" }).click();
    await expect(page.getByText("По базе знаний найдено следующее:")).toBeVisible();
    await expect(page.locator(".assistant-sources").getByText("Источники:", { exact: true })).toBeVisible();

    // Запрос на диагноз → отказ (красная линия).
    await page.getByLabel("Вопрос").fill("Поставь диагноз депрессии");
    await page.getByRole("button", { name: "Спросить" }).click();
    await expect(page.getByText("Я не могу поставить диагноз")).toBeVisible();
  });

  test("справка по препарату Авиандр — не red line, ответ с цитатой инструкции", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await page.getByRole("button", { name: "Ассистент" }).click();
    await expect(page.getByRole("heading", { name: "Ассистент врача" })).toBeVisible();

    // Справочный запрос по дозировке — НЕ отбивается red line (задача кейса 2).
    await page.getByLabel("Вопрос").fill("Какая дозировка Авиандра?");
    await page.getByRole("button", { name: "Спросить" }).click();
    await expect(page.getByText("По базе знаний найдено следующее:")).toBeVisible({ timeout: 5000 });
    // Цитата чанка препарата (АВИАНДР).
    await expect(page.locator(".assistant-sources")).toContainText(/АВИАНДР|авиандр/i);
  });

  test("@-упоминание пациента подгружает контекст в ответ", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await page.getByRole("button", { name: "Ассистент" }).click();
    await expect(page.getByRole("heading", { name: "Ассистент врача" })).toBeVisible();

    // Печатаем «@» → открывается dropdown с ротстером врача.
    const ta = page.getByLabel("Вопрос");
    await ta.fill("@");
    const dropdown = page.locator(".mention-dropdown");
    await expect(dropdown).toBeVisible({ timeout: 3000 });
    await dropdown.locator("button").first().click();

    // Чип контекста + запрос с пациентом → ответ опирается на карту пациента.
    await expect(page.locator(".patient-chip")).toContainText("Контекст пациента:");
    await page.getByRole("button", { name: "Спросить" }).click();
    await expect(page.locator(".assistant-sources").getByText("ПАЦИЕНТ")).toBeVisible({ timeout: 15000 });
  });
});
