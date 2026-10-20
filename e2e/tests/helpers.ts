// Общие e2e-хелперы: TOTP, login, регистрация пациента, согласие.
import { createHmac } from "node:crypto";

const TOTP_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
export const DEMO_TOTP = "JBSWY3DPEHPK3PXP";
export const DEMO_PASSWORD = "Demo-Pass-1";

function base32Decode(input: string): Buffer {
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const char of input.toUpperCase().replace(/=/g, "")) {
    const idx = TOTP_ALPHABET.indexOf(char);
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

export function totp(secret: string): string {
  const key = base32Decode(secret);
  const counter = Math.floor(Date.now() / 30000);
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(counter));
  const h = createHmac("sha1", key).update(buf).digest();
  const off = h[h.length - 1] & 0x0f;
  const code = ((h.readUInt32BE(off) & 0x7fffffff) % 1000000).toString().padStart(6, "0");
  return code;
}

export async function login(page, email: string, password: string, totpSecret?: string) {
  await page.goto("/");
  await page.getByRole("button", { name: "Вход" }).click();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Пароль").fill(password);
  if (totpSecret) await page.getByLabel("Код 2FA").fill(totp(totpSecret));
  await page.getByRole("button", { name: "Войти" }).click();
}

/** Уникальный email пациента для независимого теста (регистрация из UI — только PATIENT). */
export function freshPatientEmail(prefix = "e2e"): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@test.ru`;
}

/** Регистрация пациента через форму (вкладка «Регистрация»), затем авто-вход. */
export async function registerPatient(page, email: string, password = DEMO_PASSWORD) {
  await page.goto("/");
  await page.getByRole("button", { name: "Регистрация" }).click();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Пароль").fill(password);
  await page.getByRole("button", { name: "Создать аккаунт" }).click();
}
