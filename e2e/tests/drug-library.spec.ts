// Библиотека препаратов (задача кейса 6): врач ищет препарат по ГРЛС и видит
// разделы безопасности — НЯ и противопоказания (обязательное условие кейса).
import { expect, test } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, login, openSection } from "./helpers";

test.describe("Библиотека препаратов", () => {
  test("врач открывает библиотеку, ищет Авиандр и видит НЯ + противопоказания", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await openSection(page, "Библиотека");

    const search = page.getByPlaceholder("Например: Авиандр или вортиоксетин");
    await expect(search).toBeVisible();
    await search.fill("Авиандр");

    const aviandr = page.locator("tbody tr", { hasText: "Авиандр" }).first();
    await expect(aviandr).toBeVisible();
    await aviandr.getByRole("button", { name: "Открыть" }).click();

    await expect(page.getByRole("heading", { name: "Нежелательные явления" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Противопоказания" })).toBeVisible();
    await expect(page.getByText(/РУ:/)).toBeVisible();

    // Поиск, которого нет в ГРЛС, даёт пустой результат, а не ошибку.
    await page.getByRole("button", { name: "← К списку" }).click();
    await search.fill("нетакогопрепарата");
    await expect(page.getByText("Ничего не найдено.")).toBeVisible();
  });
});
