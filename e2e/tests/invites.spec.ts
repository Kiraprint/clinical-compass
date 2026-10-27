import { expect, test } from "@playwright/test";

import { DEMO_PASSWORD, DEMO_TOTP, login, openSection } from "./helpers";

/**
 * FR-49: врач заводит карту и получает одноразовую ссылку; пациент активирует
 * её в чистой сессии и попадает в кабинет пациента (экран согласия), а врач
 * видит его в реестре — ростер выдан автоматически, без /admin/roster.
 */
test("врач приглашает пациента ссылкой, пациент активирует карту", async ({ page, browser }) => {
  const stamp = Date.now();
  const fio = `Приглашённый ${stamp}`;

  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  await expect(page.getByRole("button", { name: "Меню", exact: true })).toBeVisible();
  await openSection(page, "Пациенты");
  await expect(page.getByRole("heading", { name: /Мои пациенты/ })).toBeVisible();

  // Панель приглашения: карта без аккаунта + одноразовая ссылка.
  await page.locator("summary", { hasText: "Пригласить пациента" }).click();
  await page.getByLabel("ФИО пациента").fill(fio);
  await page.getByLabel("Телефон пациента").fill("+7 926 000-11-22");
  await page.getByRole("button", { name: "Создать ссылку" }).click();

  const linkInput = page.getByLabel("Ссылка-приглашение");
  await expect(linkInput).toBeVisible();
  const link = await linkInput.inputValue();
  expect(link).toContain("/invite/");

  // Пациент открывает ссылку в чистой сессии (без cookies врача).
  const patientContext = await browser.newContext();
  const patientPage = await patientContext.newPage();
  await patientPage.goto(link);
  await expect(
    patientPage.getByRole("heading", { name: "Приглашение от клиники" }),
  ).toBeVisible();
  await expect(patientPage.getByText(fio)).toBeVisible();

  const email = `invited-${stamp}@test.ru`;
  await patientPage.getByLabel("E-mail (логин)").fill(email);
  await patientPage.getByLabel("Пароль", { exact: true }).fill("Invite-Pass-1");
  await patientPage.getByLabel("Повторите пароль").fill("Invite-Pass-1");
  await patientPage.getByRole("button", { name: "Активировать карту" }).click();

  // Активация выдаёт сессию: пациент попадает в свой кабинет (сначала согласие).
  await expect(patientPage.getByRole("button", { name: "Даю согласие" })).toBeVisible({
    timeout: 30_000,
  });
  await patientContext.close();

  // Ссылка одноразовая: повторный заход в чистой сессии — «уже не действует».
  const replayContext = await browser.newContext();
  const replayPage = await replayContext.newPage();
  await replayPage.goto(link);
  await expect(replayPage.getByText(/уже не действует|не найдена/i)).toBeVisible();
  await replayContext.close();

  // Врач видит пациента в реестре: карта и ростер созданы активацией.
  // Сессия врача в этой вкладке жива — повторный вход не нужен.
  await page.goto("/");
  await openSection(page, "Пациенты");
  await page.getByRole("link", { name: fio }).first().click();
  await expect(page.getByText("← Назад к списку")).toBeVisible();
});