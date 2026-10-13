// e2e клинического и административного интерфейса: фильтры триажа, отчёт врача, журнал аудита.
import { expect, test } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, login } from "./helpers";

test.describe("Врач и администратор", () => {
  test("врач: фильтр по триажу (GREEN и YELLOW)", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await expect(page.getByRole("button", { name: "Все" })).toBeVisible();

    await page.getByRole("button", { name: "GREEN", exact: true }).click();
    const green = page.locator("tbody tr").first();
    await expect(green).toBeVisible();
    await expect(green.locator(".badge.GREEN")).toBeVisible();

    await page.getByRole("button", { name: "YELLOW", exact: true }).click();
    const yellow = page.locator("tbody tr").first();
    await expect(yellow).toBeVisible();
    await expect(yellow.locator(".badge.YELLOW")).toBeVisible();
  });

  test("врач: отчёт RED-пациента показывает эскалации, историю и дневник", async ({ page }) => {
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await page.getByRole("button", { name: "RED", exact: true }).click();
    const cards = page.locator("tbody tr");
    await expect(cards.first()).toBeVisible();

    // Игорь К. (p2) — RED с открытыми эскалациями и записями дневника.
    await cards.first().getByRole("button", { name: "Открыть" }).click();
    await expect(page.getByText("Открытые эскалации:")).toBeVisible();
    await expect(page.getByRole("heading", { name: "История скринингов" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Дневник пациента" })).toBeVisible();
    await expect(page.locator("svg polyline")).toBeVisible(); // динамика за 90 дней
  });

  test("админ: журнал аудита содержит записи после отработанной сессии", async ({ page }) => {
    await login(page, "admin@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    await expect(page.getByRole("heading", { name: "Журнал аудита (append-only)" })).toBeVisible();
    // В симуляторе уже есть события (регистрация, согласие, скрининги, чтения отчётов).
    await expect(page.locator("tbody tr").first()).toBeVisible();
  });
});
