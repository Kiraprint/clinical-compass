import type { Page } from "@playwright/test";
import { expect, test } from "@playwright/test";

// FR-35: правовые документы — статические страницы, доступны без входа.
// Путь выбирается в main.tsx по location.pathname до рендера приложения,
// поэтому ни логина, ни запросов к API здесь быть не должно.

function collectApiCalls(page: Page): string[] {
  const calls: string[] = [];
  page.on("request", (req) => {
    const url = req.url();
    if (url.includes("/api/")) calls.push(url);
  });
  return calls;
}

test("пользовательское соглашение: без входа, плашка-черновик, ключевые тезисы", async ({ page }) => {
  const apiCalls = collectApiCalls(page);
  await page.goto("/legal/terms");

  await expect(page).toHaveTitle("Пользовательское соглашение — Компас");
  await expect(page.getByRole("heading", { level: 2, name: "Пользовательское соглашение" })).toBeVisible();
  await expect(page.locator(".alert-warn")).toContainText("требует юридической проверки");

  const main = page.locator("main.legal");
  await expect(main).toContainText("не является медицинским изделием");
  await expect(main).toContainText("не заменяет врача");
  await expect(main).toContainText("112");
  // Реквизиты оператора не выдуманы — стоят подсвеченные плейсхолдеры.
  await expect(main.locator(".placeholder").first()).toContainText("не заполнено");

  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  expect(apiCalls, `правовая страница не должна обращаться к API: ${apiCalls.join(", ")}`).toEqual([]);
});

test("согласие на обработку ПДн: состав данных, хранение, деперсонализация, отзыв", async ({ page }) => {
  const apiCalls = collectApiCalls(page);
  await page.goto("/legal/consent");

  await expect(page).toHaveTitle("Согласие на обработку персональных данных — Компас");
  await expect(page.locator(".alert-warn")).toContainText("требует юридической проверки");

  const main = page.locator("main.legal");
  await expect(main).toContainText("специальная категория");
  await expect(main).toContainText("pgp_sym_encrypt");
  await expect(main).toContainText("деперсонализация");
  await expect(main).toContainText("POST /api/v1/consent/revoke");
  await expect(main).toContainText("5 лет");
  await expect(page.getByRole("link", { name: "Пользовательское соглашение" })).toBeVisible();

  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  expect(apiCalls, `правовая страница не должна обращаться к API: ${apiCalls.join(", ")}`).toEqual([]);
});

test("правовые страницы читаемы на мобильном и возвращают в приложение", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/legal/terms");

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow, "горизонтальный скролл на мобильной ширине").toBeLessThanOrEqual(0);

  await page.getByRole("link", { name: "Согласие на обработку ПДн" }).click();
  await expect(page).toHaveURL(/\/legal\/consent$/);
  await expect(page.getByRole("heading", { level: 2, name: /Согласие на обработку/ })).toBeVisible();

  await page.getByRole("link", { name: "← Вернуться в приложение" }).click();
  await expect(page.locator('input[type="password"]')).toBeVisible();
});
