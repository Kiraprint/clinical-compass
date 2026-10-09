// e2e питч-сценария (docs/04 §6): пациент → RED-скрининг → эскалация → чат HOTLINE → врач видит RED.
import { test, expect, request } from "@playwright/test";
import { createHmac } from "node:crypto";

function base32Decode(input: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const char of input.toUpperCase().replace(/=/g, "")) {
    const idx = alphabet.indexOf(char);
    if (idx === -1) continue;
    value = (value << 5) | idx;
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return Buffer.from(out);
}

function totp(secret: string): string {
  const key = base32Decode(secret);
  const counter = Math.floor(Date.now() / 30000);
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(counter));
  const h = createHmac("sha1", key).update(buf).digest();
  const off = h[h.length - 1] & 0x0f;
  const code = ((h.readUInt32BE(off) & 0x7fffffff) % 1000000).toString().padStart(6, "0");
  return code;
}

async function login(page, email: string, password: string, totpSecret?: string) {
  await page.goto("/");
  await page.getByRole("button", { name: "Вход" }).click();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Пароль").fill(password);
  if (totpSecret) await page.getByLabel("Код 2FA").fill(totp(totpSecret));
  await page.getByRole("button", { name: "Войти" }).click();
}

test.describe("Питч-сценарий", () => {
  test("пациент: скрининг с суицидальным ответом → RED + контакты помощи", async ({ page }) => {
    await login(page, "p1@compass.demo", "Demo-Pass-1");
    await expect(page.getByRole("button", { name: "Скрининг", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Скрининг", exact: true }).click();
    await page.getByRole("button", { name: "Начать скрининг" }).click();

    const questions = page.locator(".qa");
    await expect(questions).toHaveCount(9);
    for (let i = 0; i < 9; i++) {
      const question = questions.nth(i);
      const text = await question.locator("p").innerText();
      const value = text.includes("умереть") ? 3 : 0;
      await question.getByRole("button", { name: String(value) }).click();
    }
    await page.getByRole("button", { name: "Завершить" }).click();

    await expect(page.locator(".badge.RED")).toBeVisible();
    await expect(page.getByText("Единый телефон доверия")).toBeVisible();
    await expect(page.getByText("112")).toBeVisible();
  });

  test("пациент: чат отвечает горячей линией при RED", async ({ page }) => {
    await login(page, "p2@compass.demo", "Demo-Pass-1");
    await page.getByRole("button", { name: "Чат поддержки" }).click();
    await page.locator("input").fill("Мне очень плохо");
    await page.getByRole("button", { name: "Отправить" }).click();
    await expect(page.getByText("Мы заметили, что вам сейчас тяжело")).toBeVisible();
  });

  test("врач: входит с 2FA и видит RED-пациентов", async ({ page }) => {
    await login(page, "doc@compass.demo", "Demo-Pass-1", "JBSWY3DPEHPK3PXP");
    await expect(page.getByRole("button", { name: "Все" })).toBeVisible();

    await page.getByRole("button", { name: "RED", exact: true }).click();
    const cards = page.locator("tbody tr");
    await expect(cards.first()).toBeVisible();
    await expect(cards.first().locator(".badge.RED")).toBeVisible();

    await cards.first().getByRole("button", { name: "Открыть" }).click();
    await expect(page.getByText("Последний скрининг")).toBeVisible();
  });
});