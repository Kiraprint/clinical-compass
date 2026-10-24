// Отзыв согласия (152-ФЗ): гейт возвращается, API 403/404, ПДн (дневник) уничтожены.
import { expect, test } from "@playwright/test";
import { DEMO_PASSWORD, freshPatientEmail, patientSection, registerPatient } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";

test.describe("Отзыв согласия на обработку ПДн", () => {
  test("отзыв блокирует данные, повторное согласие показывает пустой дневник", async ({ page }) => {
    const email = freshPatientEmail("revoke");
    await registerPatient(page, email);
    await page.getByRole("button", { name: "Даю согласие" }).click();
    await expect(page.getByText("Согласие активно")).toBeVisible();

    // Записываем дневник (ПДн), убеждаемся, что он виден.
    const content = `ПДн e2e ${Date.now()}`;
    // Главный экран уже открыт после согласия: дневник пишется прямо на нём.
    await page.locator("#today-diary").fill(content);
    await page.getByRole("button", { name: "Сохранить за сегодня" }).click();
    await expect(page.getByText(content)).toBeVisible();

    // Отзыв согласия → возвращается гейт 152-ФЗ, скрининг недоступен.
    await page.getByRole("button", { name: "Отозвать" }).click();
    await expect(
      page.getByRole("heading", { name: "Согласие на обработку персональных данных (152-ФЗ)" })
    ).toBeVisible();
    await expect(page.getByText("Скрининг, назначенный врачом")).toBeHidden();

    // API: активное согласие 404, скрининг/дневник — 403 CONSENT_REQUIRED.
    expect((await page.request.get(`${BASE}/api/v1/consent`)).status()).toBe(404);
    expect((await page.request.post(`${BASE}/api/v1/screenings`, { data: {} })).status()).toBe(403);
    expect((await page.request.get(`${BASE}/api/v1/diary`)).status()).toBe(403);

    // Повторное согласие → данные уничтожены: дневник пуст.
    await page.getByRole("button", { name: "Даю согласие" }).click();
    await expect(page.getByText("Согласие активно")).toBeVisible();
    await patientSection(page, "Дневник");
    await expect(page.getByText(content)).toHaveCount(0);
  });
});