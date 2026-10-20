// e2e первого запуска (регистрация → согласие → скрининг/чек-ин/дневник/тренд).
// Эти флоу недостижимы на предсозданных пациентах симулятора (они уже согласны),
// поэтому покрывают демо-вход и ежедневные действия пациента.
import { expect, test } from "@playwright/test";
import { DEMO_PASSWORD, freshPatientEmail, login, registerPatient } from "./helpers";

test.describe("Пациент: первый запуск и ежедневные действия", () => {
  test("регистрация → экран согласия → подпись → доступен скрининг", async ({ page }) => {
    await registerPatient(page, freshPatientEmail());

    // Свежий пациент без согласия — показывается гейт 152-ФЗ, скрининг недоступен.
    await expect(
      page.getByRole("heading", { name: "Согласие на обработку персональных данных" })
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Скрининг", exact: true })).toBeHidden();

    // После подписи — вкладки, скрининг доступен.
    await page.getByRole("button", { name: "Даю согласие" }).click();
    await expect(page.getByText("Согласие активно")).toBeVisible();
    await expect(page.getByRole("button", { name: "Скрининг", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Скрининг", exact: true }).click();

    // item 1: пациент не выбирает шкалу — селекта нет, назначена врачом (дефолт PHQ-9).
    await expect(page.locator("select")).toHaveCount(0);
    await expect(page.getByText("Шкала назначена вашим врачом")).toBeVisible();
    await expect(page.getByRole("button", { name: "Начать скрининг" })).toBeVisible();
  });

  test("ежедневный чек-ин: вопросы → ответы → выполнено за сегодня", async ({ page }) => {
    const email = freshPatientEmail();
    await registerPatient(page, email);
    await page.getByRole("button", { name: "Даю согласие" }).click();

    await page.getByRole("button", { name: "Сегодня", exact: true }).click();
    const questions = page.locator(".qa");
    await expect(questions).toHaveCount(3); // нет истории → первые 3 пункта шкалы
    for (let i = 0; i < 3; i++) {
      await questions.nth(i).getByRole("button", { name: "0" }).click();
    }
    await page.getByRole("button", { name: "Сохранить за сегодня" }).click();

    await expect(page.getByText("Чек-ин за сегодня выполнен")).toBeVisible();
  });

  test("дневник: запись из «Сегодня» появляется в истории", async ({ page }) => {
    await login(page, "p1@compass.demo", DEMO_PASSWORD);
    await page.getByRole("button", { name: "Сегодня", exact: true }).click();

    const content = `Запись e2e ${Date.now()}`;
    await page.locator("#today-diary").fill(content);
    await page.getByRole("button", { name: "Сохранить за сегодня" }).click();
    await expect(page.getByText(content)).toBeVisible();

    await page.getByRole("button", { name: "Дневник" }).click();
    await expect(page.getByText(content)).toBeVisible();
  });

  test("тренд пациента: график и последний балл при наличии истории", async ({ page }) => {
    await login(page, "p1@compass.demo", DEMO_PASSWORD);
    await page.getByRole("button", { name: "Мой тренд" }).click();

    // 14 скринингов → polyline рисуется, есть текст с последним баллом и дельтой.
    await expect(page.locator("svg polyline")).toBeVisible();
    await expect(page.getByText(/Последний балл: \d+/)).toBeVisible();
    await expect(page.getByText(/Дельта за 30 дней/)).toBeVisible();
  });
});
