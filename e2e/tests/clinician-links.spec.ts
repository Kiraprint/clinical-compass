import { expect, test } from "@playwright/test";
import { login, DEMO_PASSWORD, DEMO_TOTP, openSection } from "./helpers";

// Карточка пациента — адресуемая ссылка #patient=<id>: из списка, из уведомления
// об эскалации и по прямой ссылке (её можно отправить коллеге).

test("имя пациента в списке даёт ссылку #patient и открывает карточку", async ({ page }) => {
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await openSection(page, "Пациенты");
  await expect(page.getByRole("button", { name: "Все" })).toBeVisible();

  const link = page.locator('a[href^="#patient="]').first();
  const href = await link.getAttribute("href");
  expect(href).toMatch(/^#patient=[0-9a-f-]{36}$/);
  const name = (await link.innerText()).trim();

  await link.click();
  await expect(page).toHaveURL(new RegExp(href!.replace("#", "#").replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  await expect(page.getByText("← Назад к списку")).toBeVisible();
  await expect(page.locator("body")).toContainText(name);

  // прямая ссылка в новой загрузке открывает ту же карточку
  await page.goto("/" + href);
  await expect(page.getByText("← Назад к списку")).toBeVisible();
  await expect(page.locator("body")).toContainText(name);

  // «Назад к списку» возвращает к таблице пациентов и снимает hash
  await page.getByText("← Назад к списку").click();
  await expect(page.getByRole("button", { name: "Все" })).toBeVisible();
  expect(new URL(page.url()).hash).toBe("");
});

test("клик по уведомлению об эскалации открывает карточку пациента", async ({ page }) => {
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await openSection(page, "Пациенты");
  await expect(page.getByRole("button", { name: "Все" })).toBeVisible();

  await page.getByRole("button", { name: "Уведомления" }).click();
  const item = page.locator(".bell-item").filter({ hasText: "RED-эскалация" }).first();
  const text = (await item.innerText()).replace(/\s+/g, " ");
  const patientName = text.split(":")[0].replace(/^RED-эскалация\s*/, "").replace(/-?\s*RED-эскалация$/, "").trim();
  await item.click();

  await expect(page).toHaveURL(/#patient=[0-9a-f-]{36}$/);
  await expect(page.getByText("← Назад к списку")).toBeVisible();
  const body = (await page.locator("body").innerText()).replace(/\s+/g, " ");
  expect(body).toContain(patientName.split(" ")[0]);
});

// FR-47: карточка пациента — длинная простыня блоков. Липкая строка чипов
// прокручивает к нужному блоку, попутно раскрывая свёрнутые.
test("оглавление карточки: чип «Динамика» раскрывает блок и прокручивает к нему", async ({
  page,
}) => {
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await openSection(page, "Пациенты");
  await page.locator('a[href^="#patient="]').first().click();
  await expect(page.getByText("← Назад к списку")).toBeVisible();

  const nav = page.getByRole("navigation", { name: "Разделы карточки пациента" });
  await expect(nav).toBeVisible();
  for (const label of ["Риск", "Скрининг", "Чек-ины", "Лекарства", "НЯ", "История", "Динамика"]) {
    await expect(nav.getByRole("button", { name: label, exact: true })).toBeVisible();
  }

  // «Объединённая история» свёрнута по умолчанию, но чип её раскрывает.
  const history = page.locator("#clinician-history");
  await expect(history).toHaveJSProperty("open", false);
  await nav.getByRole("button", { name: "История", exact: true }).click();
  await expect(history).toHaveJSProperty("open", true);

  const dynamics = page.locator("#patient-dynamics");
  await expect(dynamics).toHaveJSProperty("open", false);
  const chip = nav.getByRole("button", { name: "Динамика", exact: true });
  await chip.click();
  await expect(chip).toHaveAttribute("aria-current", "true");
  await expect(dynamics).toHaveJSProperty("open", true);

  // Прокрутили: страница ушла вниз, а блок стоит в окне, не под липкими чипами.
  const viewport = page.viewportSize()!;
  await expect
    .poll(async () => (await dynamics.boundingBox())?.y ?? -1, { timeout: 5000 })
    .toBeGreaterThanOrEqual(0);
  await expect
    .poll(async () => (await dynamics.boundingBox())?.y ?? Infinity, { timeout: 5000 })
    .toBeLessThan(viewport.height / 2);
  expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
});
