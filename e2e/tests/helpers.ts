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
  if (totpSecret) {
    // Поле 2FA рендерится, только когда GET /auth/config вернул require_otp.
    // На стендах с CC_REQUIRE_OTP=false его нет — входим без кода.
    const field = page.getByLabel("Код 2FA");
    try {
      await field.waitFor({ state: "visible", timeout: 3000 });
      await field.fill(totp(totpSecret));
    } catch {
      // 2FA на стенде отключён.
    }
  }
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

/** FR-33: разделы пациента живут в меню «3 полоски», а не в таб-баре. */
export async function openPatientMenu(page) {
  await page.getByRole("button", { name: "Меню", exact: true }).click();
}

/** Переход в раздел кабинета пациента через меню. */
export async function patientSection(page, label: string) {
  await openPatientMenu(page);
  await page.getByRole("button", { name: label, exact: true }).click();
}

/** Скрининг открывается с главного экрана баннером «Скрининг, назначенный врачом». */
export async function openScreening(page) {
  await page.getByRole("button", { name: "Перейти к скринингу" }).click();
}

/** Чат — плашка в правом нижнем углу. */
export async function openPatientChat(page) {
  await page.getByRole("button", { name: "Чат с врачом" }).click();
}

/**
 * FR-32: левое меню врача спрятано под «три полоски». Открываем (если закрыто)
 * и выбираем раздел по точному имени.
 */
export async function openSection(page, name: string) {
  const toggle = page.getByRole("button", { name: "Меню", exact: true });
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await page
    .getByRole("navigation", { name: "Разделы кабинета врача" })
    .getByRole("button", { name, exact: true })
    .click();
}

/**
 * Свежие врач и пациент с согласием и roster: свой врач — свои слоты, спеки не мешают
 * друг другу. `request` — APIRequestContext теста.
 */
export async function setupClinicianPair(request, prefix = "csp") {
  const base = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
  const j = (resp) => resp.json();
  const docReg = await j(
    await request.post(`${base}/api/v1/auth/register`, {
      data: { email: freshPatientEmail(`${prefix}doc`), password: DEMO_PASSWORD, role: "CLINICIAN" },
    }),
  );
  const patReg = await j(
    await request.post(`${base}/api/v1/auth/register`, {
      data: { email: freshPatientEmail(`${prefix}pat`), password: DEMO_PASSWORD, role: "PATIENT" },
    }),
  );
  const patTok = (
    await j(
      await request.post(`${base}/api/v1/auth/login`, {
        data: { email: patReg.email, password: DEMO_PASSWORD },
      }),
    )
  ).access_token as string;
  await request.post(`${base}/api/v1/consent`, {
    data: { version: 1 },
    headers: { Authorization: `Bearer ${patTok}` },
  });
  const admTok = (
    await j(
      await request.post(`${base}/api/v1/auth/login`, {
        data: { email: "admin@compass.demo", password: DEMO_PASSWORD, otp: totp(DEMO_TOTP) },
      }),
    )
  ).access_token as string;
  await request.post(`${base}/api/v1/admin/roster`, {
    data: { patient_id: patReg.id, clinician_id: docReg.id },
    headers: { Authorization: `Bearer ${admTok}` },
  });
  return { docReg, patReg };
}
