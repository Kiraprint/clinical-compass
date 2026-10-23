// Единый ежедневный экран «Сегодня»: пошаговый адаптивный чек-ин (docs/16), дневник
// с 5-точечной шкалой настроения и отметка о приёме лекарств.
import { expect, test, type Page } from "@playwright/test";
import { freshPatientEmail, registerPatient } from "./helpers";

// Ядро чек-ина: MO1, TR1, SL1, IN1, EN1, FU1 и gate-пункт HR4 (docs/16 §1).
const CORE_ITEMS = 7;

async function openToday(page: Page) {
  await registerPatient(page, freshPatientEmail());
  await page.getByRole("button", { name: "Даю согласие" }).click();
  await expect(page.getByRole("heading", { name: "Сегодня" })).toBeVisible();
}

/** Пункты текущего шага отрисованы и шаг можно отправлять (кнопка активна). */
async function waitForStepSettled(page: Page) {
  await expect
    .poll(
      async () => {
        if (await page.getByText("Чек-ин за сегодня выполнен").isVisible()) return "done";
        const next = page
          .getByRole("button", { name: "Далее" })
          .or(page.getByRole("button", { name: "Отправить" }));
        if ((await next.count()) === 0) return "busy";
        return (await next.isDisabled()) ? "ready" : "busy";
      },
      { timeout: 20_000 },
    )
    .not.toBe("busy");
}

/**
 * Отвечает на все пункты текущего шага. Первая кнопка — значение 0 («Совсем нет» / «Нет»);
 * yesGate — для gate-пункта HR4 выбрать «Да» (вторая кнопка).
 */
async function answerStep(page: Page, yesGate = false) {
  const questions = page.locator(".qa");
  const count = await questions.count();
  expect(count).toBeGreaterThan(0);
  for (let i = 0; i < count; i++) {
    const question = questions.nth(i);
    const isGate = (await question.getAttribute("data-item")) === "HR4";
    await question.getByRole("button").nth(yesGate && isGate ? 1 : 0).click();
  }
}

async function nextStep(page: Page) {
  await page.getByRole("button", { name: /^(Далее|Отправить)$/ }).click();
}

/** Проходит чек-ин до конца: ядро → (безопасность | модули) → «выполнен». */
async function completeCheckin(page: Page, yesGate = false) {
  await page.getByRole("button", { name: "Начать чек-ин" }).click();
  for (let step = 0; step < 6; step++) {
    await waitForStepSettled(page);
    if (await page.getByText("Чек-ин за сегодня выполнен").isVisible()) return;
    await answerStep(page, yesGate && step === 0);
    await nextStep(page);
  }
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toBeVisible();
}

test("«Сегодня»: первый шаг — 7 пунктов ядра; неполный чек-ин не блокирует дневник и приём", async ({
  page,
}) => {
  await openToday(page);
  await expect(page.getByRole("heading", { name: "Чек-ин" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Дневник" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Приём лекарств" })).toBeVisible();

  await page.getByRole("button", { name: "Начать чек-ин" }).click();
  await expect(page.locator(".qa")).toHaveCount(CORE_ITEMS);
  await expect(page.locator('[data-item="HR4"]')).toBeVisible();

  // Отвечен только один пункт шага — «Далее» заблокирована.
  await page.locator(".qa").first().getByRole("button").first().click();
  await expect(page.getByRole("button", { name: "Далее" })).toBeDisabled();

  // Дневник: настроение — 5 кнопок 0..4, сон — часы.
  const content = `День прошёл ровно ${Date.now()}`;
  await page.locator("#today-diary").fill(content);
  await expect(page.locator(".mood").getByRole("button")).toHaveCount(5);
  await page.locator(".mood").getByRole("button", { name: /Отлично/ }).click();
  await page.getByRole("button", { name: "Принял", exact: true }).click();
  await page.getByPlaceholder("Препарат (напр. Авиандр) — необязательно").fill("Авиандр");
  await page.getByRole("button", { name: "Сохранить за сегодня" }).click();

  // Неполный чек-ин не мешает сохранить дневник и приём лекарств.
  await expect(page.getByText("Сохранено: дневник · приём лекарств")).toBeVisible();
  await expect(page.getByText(content)).toBeVisible();
  await expect(page.locator(".msg", { hasText: content }).getByText("😄 Отлично")).toBeVisible();
  await expect(page.getByText(/За сегодня отмечено: Принял/)).toBeVisible();
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Далее" })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("button", { name: "Далее" })).toBeVisible();
  await expect(page.getByText(content)).toBeVisible();
  await expect(page.getByRole("button", { name: "Начать чек-ин" })).toHaveCount(0);
});

test("полный чек-ин: шаги до «Чек-ин за сегодня выполнен», состояние переживает перезагрузку", async ({
  page,
}) => {
  await openToday(page);
  await completeCheckin(page, false);

  await expect(page.getByText("Чек-ин за сегодня выполнен")).toBeVisible();
  await expect(page.getByText(/видит врач в карте/)).toBeVisible();
  await expect(page.locator(".qa")).toHaveCount(0);

  await page.reload();
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toBeVisible();
  await expect(page.getByRole("button", { name: "Начать чек-ин" })).toHaveCount(0);
});

test("ответ «Да» на HR4 ведёт на шаг безопасности, RS3 = «Нет» — экстренный блок", async ({ page }) => {
  await openToday(page);
  await page.getByRole("button", { name: "Начать чек-ин" }).click();
  await waitForStepSettled(page);
  await answerStep(page, true);
  await nextStep(page);

  // Модуль безопасности RS1–RS3, симптоматические модули в этот день не предъявляются.
  await expect(page.locator(".qa")).toHaveCount(3);
  await expect(page.locator('[data-item="RS1"]')).toBeVisible();
  await expect(page.locator('[data-item="RS3"]')).toBeVisible();

  // RS1 = «Нет», RS2 = «Нет», RS3 = «Нет» → срочный сигнал.
  await answerStep(page);
  await nextStep(page);

  await expect(page.getByText("Чек-ин за сегодня выполнен")).toBeVisible();
  await expect(page.getByText("Ваша безопасность важнее всего")).toBeVisible();
  await expect(page.getByText(/обратитесь за экстренной помощью/)).toBeVisible();

  // Блок безопасности виден и после перезагрузки.
  await page.reload();
  await expect(page.getByText("Ваша безопасность важнее всего")).toBeVisible();
});

test("прогресс чек-ина восстанавливается после перезагрузки", async ({ page }) => {
  await openToday(page);
  await page.getByRole("button", { name: "Начать чек-ин" }).click();
  await waitForStepSettled(page);
  await answerStep(page, false);
  await nextStep(page);
  await waitForStepSettled(page);

  // Ядро отвечено целиком — следующий шаг и прогресс пришли с сервера.
  await expect(page.getByText(/Прогресс: 7 из \d+/)).toBeVisible();
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toHaveCount(0);

  await page.reload();
  await expect(page.getByText(/Прогресс: 7 из \d+/)).toBeVisible();
  await expect(page.getByText("Чек-ин за сегодня выполнен")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Начать чек-ин" })).toHaveCount(0);
});
