import { expect, test } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, login, openSection } from "./helpers";

/**
 * Запись приёма уходит на сервер и расшифровка стартует без нажатий: врач
 * останавливает запись и больше ничего не делает (жалоба с приёмки — «надо было
 * жать Загрузить и Отправить»). Фейковый микрофон Chromium даёт реальный поток,
 * поэтому тест не зависит от железа, а ML на стенде может быть выключен — тогда
 * проверяем попытку расшифровки (статус ушёл с «off»), а не её успех.
 */
test.use({
  permissions: ["microphone"],
  launchOptions: {
    args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
  },
});

test("запись уходит и расшифровка стартует без кнопок", async ({ page }) => {
  await page.goto("/");
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await expect(page.getByRole("button", { name: "Меню", exact: true })).toBeVisible({ timeout: 20000 });
  await openSection(page, "Расписание");
  await page.getByRole("button", { name: "Открыть приём" }).first().click();
  await expect(page.getByRole("heading", { name: /^Приём:/ })).toBeVisible({ timeout: 20000 });
  // Стенд с CC_SESSION_AI_ENABLED=false (так стартует CI) не показывает запись вовсе:
  // тогда проверять нечего, и это честный skip, а не зелёный результат.
  test.skip(
    await page.getByText("ИИ-ассистент выключен администратором").isVisible(),
    "на стенде выключен CC_SESSION_AI_ENABLED",
  );
  // Карточка ИИ — <details>: скрытые кнопки не находятся по роли, раскрываем через DOM.
  await page.evaluate(() => {
    document.querySelectorAll("details").forEach((d) => {
      d.open = true;
    });
  });
  const consent = page.getByLabel("Согласие пациента на аудиозапись");
  if (!(await consent.isChecked())) await consent.click({ force: true });

  const rec = page.getByRole("button", { name: /Начать запись|Разрешить микрофон/ });
  await expect(rec).toBeVisible({ timeout: 15000 });
  await rec.click();
  await expect(page.getByRole("button", { name: /Остановить запись/ })).toBeVisible({ timeout: 30000 });
  await page.waitForTimeout(2500);
  await page.getByRole("button", { name: /Остановить запись/ }).click();

  await expect(page.getByText(/Запись загружена/)).toBeVisible({ timeout: 40000 });
  await expect(page.getByText(/Статус: (расшифровка|идёт)/)).toBeVisible({ timeout: 40000 });
  await expect(page.getByRole("button", { name: "Отправить ещё раз" })).toHaveCount(0);
});
