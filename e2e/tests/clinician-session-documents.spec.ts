// FR-39/40/41: документ приёма — подпись, проверка, PDF и передача в ЕМИАС
// (docs/18 §4.3, §7, §8). ИИ-ассистент в стеке CI выключен (`CC_SESSION_AI_ENABLED=0`),
// поэтому проверяется его информационное состояние без мёртвых кнопок.
import { statSync } from "node:fs";
import { expect, test } from "@playwright/test";
import { DEMO_PASSWORD, openSection, setupClinicianPair, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";

test("протокол подписывается, проверяется, печатается и регистрируется в РЭМД", async ({
  page,
  request,
}, testInfo) => {
  const { docReg, patReg } = await setupClinicianPair(request, "sdoc");

  await page.goto(`${BASE}/`);
  await page.getByLabel("Email").fill(docReg.email);
  await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
  await page.getByLabel("Код 2FA").fill(totp(docReg.totp_secret as string));
  await page.getByRole("button", { name: "Войти" }).click();
  await openSection(page, "Расписание");
  await expect(page.getByRole("heading", { name: "Расписание" })).toBeVisible();

  // Приём без записи: панель сессии без календарных конфликтов.
  await page.getByRole("button", { name: "Начать приём без записи" }).click();
  await page.getByLabel("Пациент без записи").selectOption(patReg.id);
  await page.getByRole("button", { name: "Начать приём сейчас" }).click();
  await expect(page.getByRole("heading", { name: /^Приём:/ })).toBeVisible();

  // §4.3: ИИ выключен администратором — карточка объясняет это, кнопок записи нет.
  await expect(page.getByText("ИИ-ассистент выключен администратором")).toBeVisible();
  await expect(page.getByRole("button", { name: "Начать запись" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Сформировать черновик протокола" })).toHaveCount(0);

  // §7/§8: до подтверждения документ объясняет, чего не хватает, вместо мёртвых кнопок.
  await expect(page.getByText("Следующий шаг — «Подтверждён врачом»")).toBeVisible();
  await expect(page.getByRole("button", { name: "Подписать", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Скачать PDF" })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("document-unsigned.png"), fullPage: true });

  // Обязательные разделы §5.2 → черновик → подтверждение.
  await page.getByLabel("Правовое основание (ИДС)").fill("ИДС № 41 от 2026-10-26");
  await page.getByLabel("Жалобы").fill("Тревога, нарушения сна, снижение рабочего тонуса.");
  await page.getByLabel("Суицидальный риск (обязательно)").fill("Не выявлен, суицидальных мыслей нет.");
  await page.getByLabel("МКБ-10").fill("F41.1");
  await page.getByRole("button", { name: "Сохранить черновик" }).click();
  await expect(page.getByText(/Черновик сохранён \(версия/)).toBeVisible();
  await page.getByRole("button", { name: "Подтвердить протокол" }).click();
  await expect(page.getByText("Протокол подтверждён.")).toBeVisible();

  // Подтверждённая версия: следующий шаг — подпись, кнопка доступна.
  await expect(page.getByText("Следующий шаг — «Подписан УКЭП (врач + МО)»")).toBeVisible();
  const signButton = page.getByRole("button", { name: "Подписать", exact: true });
  await expect(signButton).toBeEnabled();
  await signButton.click();

  // Две подписи (§7): врач и МО, у каждой — реквизиты сертификата.
  const blocks = page.locator(".signature-block");
  await expect(blocks).toHaveCount(2);
  await expect(blocks.first()).toContainText("ПОДПИСАНО");
  await expect(blocks.nth(1)).toContainText("Медицинская организация");
  await expect(
    page.getByText("Демонстрационная подпись: УКЭП аккредитованного УЦ не выдавалась."),
  ).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("protocol-signed.png"), fullPage: true });

  // §7.4: проверка подписи — статус-чип «действительна» + хэш ГОСТ.
  await page.getByRole("button", { name: "Проверить подпись" }).click();
  await expect(page.locator(".badge").filter({ hasText: "подпись действительна" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("signature-verified.png"), fullPage: true });

  // §7.5: печатная форма — непустой PDF, имя файла по версии протокола.
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Скачать PDF" }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^protocol-\d+\.pdf$/);
  const pdfPath = await download.path();
  expect(pdfPath).toBeTruthy();
  expect(statSync(pdfPath as string).size).toBeGreaterThan(1000);
  await download.saveAs(testInfo.outputPath("protocol.pdf"));

  // §8: передача в ЕМИАС — 202, затем опрос доставки до `registered`.
  await page.getByRole("button", { name: "Передать в ЕМИАС" }).click();
  await expect(page.locator(".badge").filter({ hasText: "зарегистрирован в РЭМД" })).toBeVisible({
    timeout: 60_000,
  });
  await expect(page.getByRole("button", { name: "Передать в ЕМИАС" })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("emias-registered.png"), fullPage: true });
});
