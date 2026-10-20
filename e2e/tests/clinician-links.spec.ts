import { expect, test } from "@playwright/test";
import { login, DEMO_PASSWORD, DEMO_TOTP } from "./helpers";

// Карточка пациента — адресуемая ссылка #patient=<id>: из списка, из уведомления
// об эскалации и по прямой ссылке (её можно отправить коллеге).

test("имя пациента в списке даёт ссылку #patient и открывает карточку", async ({ page }) => {
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
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
  await expect(page.getByRole("button", { name: "Все" })).toBeVisible();

  await page.getByRole("button", { name: "Уведомления" }).click();
  const item = page.locator(".bell-item").first();
  const text = (await item.innerText()).replace(/\s+/g, " ");
  const patientName = text.split(":")[0].replace(/^RED-эскалация\s*/, "").replace(/-?\s*RED-эскалация$/, "").trim();
  await item.click();

  await expect(page).toHaveURL(/#patient=[0-9a-f-]{36}$/);
  await expect(page.getByText("← Назад к списку")).toBeVisible();
  const body = (await page.locator("body").innerText()).replace(/\s+/g, " ");
  expect(body).toContain(patientName.split(" ")[0]);
});
