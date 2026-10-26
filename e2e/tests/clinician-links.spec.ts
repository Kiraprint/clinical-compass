import { expect, test } from "@playwright/test";
import { login, DEMO_PASSWORD, DEMO_TOTP, openSection } from "./helpers";

// Карточка пациента — адресуемая ссылка #patient=<id>: из списка, из уведомления
// об эскалации и по прямой ссылке (её можно отправить коллеге).

test("имя пациента в списке даёт ссылку #patient и открывает карточку", async ({ page }) => {
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await openSection(page, "Пациенты");
  await expect(page.getByRole("button", { name: "Все" })).toBeVisible();

  const link = page.locator('table.patients a[href^="#patient="]').first();
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
  await page.locator('table.patients a[href^="#patient="]').first().click();
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

  // Липкая строка чипов не накрывает заголовок блока (scroll-margin-top).
  const navBox = (await nav.boundingBox())!;
  const box = (await dynamics.boundingBox())!;
  expect(box.y).toBeGreaterThanOrEqual(navBox.y + navBox.height - 1);
});

test("оглавление карточки: на 390px чипы скроллятся вбок одной строкой", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 780 });
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await openSection(page, "Пациенты");
  await page.locator('table.patients a[href^="#patient="]').first().click();
  await expect(page.getByText("← Назад к списку")).toBeVisible();

  const nav = page.getByRole("navigation", { name: "Разделы карточки пациента" });
  await expect(nav).toBeVisible();
  const metrics = await nav.evaluate((el) => ({
    scrollWidth: el.scrollWidth,
    clientWidth: el.clientWidth,
    height: el.getBoundingClientRect().height,
  }));
  // Прокрутка вбок вместо переноса строк: ширина контента больше окна, высота — одна строка.
  expect(metrics.scrollWidth).toBeGreaterThan(metrics.clientWidth);
  expect(metrics.height).toBeLessThan(60);

  // Липкость сохраняется: после прокрутки строка чипов прижата к верху окна.
  await page.evaluate(() => window.scrollTo(0, 600));
  await expect.poll(async () => (await nav.boundingBox())?.y ?? -1).toBe(0);
});

test("оглавление карточки: липкая кнопка «Меню» не накрывает чипы на 768/1001/1280px", async ({
  page,
}) => {
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await openSection(page, "Пациенты");
  await page.locator('table.patients a[href^="#patient="]').first().click();
  await expect(page.getByText("← Назад к списку")).toBeVisible();
  // openSection открывает меню кабинета: на узкой ширине это оверлей поверх
  // карточки. Закрываем, иначе меряем попадание в оверлей, а не в чипы.
  if (
    (await page.getByRole("button", { name: "Меню", exact: true }).getAttribute("aria-expanded")) ===
    "true"
  ) {
    await page.getByRole("button", { name: "Скрыть меню" }).click();
  }

  const nav = page.getByRole("navigation", { name: "Разделы карточки пациента" });
  for (const width of [768, 1001, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await page.evaluate(() => window.scrollTo(0, 600));
    const chip = nav.getByRole("button", { name: "Риск", exact: true });
    // Кнопка «☰ Меню» липнет слева: точка в центре первого чипа — сам чип, а не кнопка.
    await expect
      .poll(async () => {
        const box = await chip.boundingBox();
        if (!box) return "нет чипа";
        return page.evaluate(
          ({ x, y }) => (document.elementFromPoint(x, y)?.textContent ?? "").trim(),
          { x: box.x + box.width / 2, y: box.y + box.height / 2 },
        );
      })
      .toBe("Риск");
  }
});
