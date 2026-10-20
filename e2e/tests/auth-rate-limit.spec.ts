// Rate limit на /auth/login: перебор пароля/2FA → 429 TOO_MANY_REQUESTS.
// Ключ лимита — (IP, email), поэтому легитимный вход другим аккаунтом не блокируется.
import { expect, test } from "@playwright/test";
import { DEMO_PASSWORD, freshPatientEmail, login, registerPatient } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";

test.describe("Rate limit /auth/login", () => {
  test("после 5 неудачных попыток 6-я блокируется 429, другой аккаунт входит", async ({ page, request }) => {
    const email = freshPatientEmail("rl");
    await registerPatient(page, email);
    // Выходим, чтобы вернуться к форме входа.
    await page.getByRole("button", { name: "Выйти" }).click();

    // 5 неудачных попыток: каждая показывает 401-ошибку.
    for (let i = 0; i < 5; i++) {
      await login(page, email, "Wrong-Pass-9");
      await expect(page.getByText("Неверный email или пароль")).toBeVisible();
    }

    // 6-я попытка — 429, лимит исчерпан.
    await login(page, email, "Wrong-Pass-9");
    await expect(page.getByText(/Слишком много попыток входа/)).toBeVisible();

    // Другой аккаунт (другой email, тот же IP) входит без проблем.
    const r = await request.post(`${BASE}/api/v1/auth/login`, {
      data: { email: "p1@compass.demo", password: DEMO_PASSWORD },
    });
    expect(r.status()).toBe(200);
  });
});