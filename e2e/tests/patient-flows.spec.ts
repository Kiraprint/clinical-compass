// e2e первого запуска (регистрация → согласие → скрининг/чек-ин/дневник/тренд).
// Эти флоу недостижимы на предсозданных пациентах симулятора (они уже согласны),
// поэтому покрывают демо-вход и ежедневные действия пациента.
import { expect, test } from "@playwright/test";
import {
  DEMO_PASSWORD,
  freshPatientEmail,
  login,
  openScreening,
  patientSection,
  registerPatient,
} from "./helpers";

test.describe("Пациент: первый запуск и ежедневные действия", () => {
  test("регистрация → экран согласия → подпись → доступен скрининг", async ({ page }) => {
    await registerPatient(page, freshPatientEmail());

    // Свежий пациент без согласия — показывается гейт 152-ФЗ, скрининг недоступен.
    await expect(
      page.getByRole("heading", { name: "Согласие на обработку персональных данных" })
    ).toBeVisible();
    await expect(page.getByText("Скрининг, назначенный врачом")).toBeHidden();

    // После подписи — главный экран с баннером скрининга (FR-33 A).
    await page.getByRole("button", { name: "Даю согласие" }).click();
    await expect(page.getByText("Согласие активно")).toBeVisible();
    await expect(page.getByText("Скрининг, назначенный врачом")).toBeVisible();
    await openScreening(page);

    // item 1: пациент не выбирает шкалу — селекта нет, назначена врачом (дефолт PHQ-9).
    await expect(page.locator("select")).toHaveCount(0);
    await expect(page.getByText("Шкала назначена вашим врачом")).toBeVisible();
    await expect(page.getByRole("button", { name: "Начать скрининг" })).toBeVisible();
  });

  test("ежедневный чек-ин: шаги → ответы → выполнено за сегодня", async ({ page }) => {
    const email = freshPatientEmail();
    await registerPatient(page, email);
    await page.getByRole("button", { name: "Даю согласие" }).click();

    // Главный экран — это и есть «Сегодня»: чек-ин сразу под напоминаниями.
    await page.getByRole("button", { name: "Начать чек-ин" }).click();

    // Первый шаг — 6 пунктов ядра v2.0.0 (docs/16 §1); отвечаем первым вариантом.
    await expect(page.locator(".qa")).toHaveCount(6);
    for (let step = 0; step < 5; step++) {
      const questions = page.locator(".qa");
      const count = await questions.count();
      for (let i = 0; i < count; i++) {
        await questions.nth(i).getByRole("button").first().click();
      }
      const [resp] = await Promise.all([
        page.waitForResponse((r) => r.url().includes("/answers") && r.request().method() === "POST"),
        page.getByRole("button", { name: /^(Далее|Отправить)$/ }).click(),
      ]);
      expect(resp.ok()).toBeTruthy();
      const body = (await resp.json()) as { status: string };
      if (body.status === "completed") break;
      await expect(page.locator(".qa").first()).toBeVisible();
    }

    await expect(page.getByText("Чек-ин за сегодня выполнен")).toBeVisible();
  });

  test("дневник: запись из «Сегодня» появляется в истории", async ({ page }) => {
    await login(page, "p1@compass.demo", DEMO_PASSWORD);

    const content = `Запись e2e ${Date.now()}`;
    await page.locator("#today-diary").fill(content);
    await page.getByRole("button", { name: "Сохранить за сегодня" }).click();
    await expect(page.getByText(content)).toBeVisible();

    await patientSection(page, "Дневник");
    await expect(page.getByText(content)).toBeVisible();
  });

  test("тренд пациента: графики по шкалам с точками истории", async ({ page }) => {
    await login(page, "p1@compass.demo", DEMO_PASSWORD);
    await patientSection(page, "Мой тренд");
    // Пункт «Мой тренд» рисует отдельный SVG на шкалу (FR-33); у p1 есть история PHQ-9.
    await expect(page.getByRole("img", { name: /PHQ-9: \d+ точек/ }).first()).toBeVisible();
    await expect(page.locator("svg polyline").first()).toBeVisible();
  });
});
