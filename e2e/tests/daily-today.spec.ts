// Единый ежедневный экран «Сегодня»: чек-ин + дневник + приём лекарств одним сохранением.
import { expect, test } from "@playwright/test";
import { freshPatientEmail, registerPatient } from "./helpers";

test("«Сегодня»: чек-ин, дневник и приём лекарств сохраняются одной кнопкой", async ({ page }) => {
  const email = freshPatientEmail();
  await registerPatient(page, email);
  await page.getByRole("button", { name: "Даю согласие" }).click();

  // «Сегодня» — вкладка по умолчанию, все три блока на одном экране.
  await expect(page.getByRole("heading", { name: "Сегодня" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Чек-ин" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Дневник" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Приём лекарств" })).toBeVisible();

  const questions = page.locator(".qa");
  await expect(questions).toHaveCount(3);
  for (let i = 0; i < 3; i++) {
    await questions.nth(i).getByRole("button", { name: "0" }).click();
  }

  const content = `День прошёл ровно ${Date.now()}`;
  await page.locator("#today-diary").fill(content);
  await page.getByRole("button", { name: "Принял", exact: true }).click();
  await page.getByPlaceholder("Препарат (напр. Авиандр) — необязательно").fill("Авиандр");
  await page.getByRole("button", { name: "Сохранить за сегодня" }).click();

  // Одно сохранение — все три части.
  await expect(page.getByText(/Сохранено: .*чек-ин.*дневник.*приём лекарств/)).toBeVisible();
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toBeVisible();
  await expect(page.getByText(content)).toBeVisible();
  await expect(page.getByText(/За сегодня отмечено: Принял/)).toBeVisible();

  // Состояние переживает перезагрузку: чек-ин и приём уже зафиксированы.
  await page.reload();
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toBeVisible();
  await expect(page.getByText(/За сегодня отмечено: Принял/)).toBeVisible();
  await expect(page.getByText(content)).toBeVisible();

  // История приверженности видит сегодняшнюю отметку.
  await page.getByRole("button", { name: "Приверженность", exact: true }).click();
  await expect(page.getByText(/Приверженность: 100%/)).toBeVisible();
  await expect(page.getByText(/Принял · Авиандр/)).toBeVisible();
});

test("«Сегодня»: неполный чек-ин не блокирует дневник и отметку о приёме", async ({ page }) => {
  const email = freshPatientEmail();
  await registerPatient(page, email);
  await page.getByRole("button", { name: "Даю согласие" }).click();

  const content = `Черновик ${Date.now()}`;
  await page.locator("#today-diary").fill(content);
  await page.locator(".qa").first().getByRole("button", { name: "1" }).click();
  await page.getByRole("button", { name: "Принял", exact: true }).click();
  await page.getByRole("button", { name: "Сохранить за сегодня" }).click();

  // Дневник и приём сохранены, чек-ин — нет: подсказка, сколько вопросов осталось.
  await expect(page.getByText(/Сохранено: дневник · приём лекарств/)).toBeVisible();
  await expect(page.getByText(/Чек-ин не сохранён: ответьте ещё на 2/)).toBeVisible();
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toHaveCount(0);
  await expect(page.getByText(content)).toBeVisible();
});
