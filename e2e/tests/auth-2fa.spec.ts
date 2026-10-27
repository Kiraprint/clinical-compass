// Краевые случаи 2FA: врач/админ обязаны ввести корректный TOTP, пациент — нет.
import { expect, test, type APIResponse } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, login, totp } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: APIResponse) => r.json();

/** Одноразовый врач: негативные 2FA-попытки НЕ должны расходовать
 *  бюджет rate-limit общего doc@compass.demo (5 неудач/60с на IP+email). */
async function freshClinician(request) {
  const email = `e2e-2fa-${Date.now()}-${Math.floor(Math.random() * 1e6)}@test.ru`;
  const reg = await request.post(`${BASE}/api/v1/auth/register`, {
    data: { email, password: DEMO_PASSWORD, role: "CLINICIAN" },
  });
  expect(reg.status()).toBe(201);
  const body = await reg.json();
  return { email, totpSecret: body.totp_secret as string };
}

test.describe("2FA: краевые случаи", () => {
  test("API: без OTP и с неверным OTP врач не входит (401 UNAUTHORIZED)", async ({ request }) => {
    const doc = await freshClinician(request);
    const noOtp = await request.post(`${BASE}/api/v1/auth/login`, {
      data: { email: doc.email, password: DEMO_PASSWORD },
    });
    expect(noOtp.status()).toBe(401);
    expect((await j(noOtp)).code).toBe("UNAUTHORIZED");

    const wrongOtp = await request.post(`${BASE}/api/v1/auth/login`, {
      data: { email: doc.email, password: DEMO_PASSWORD, otp: "000000" },
    });
    expect(wrongOtp.status()).toBe(401);

    // Корректный TOTP — вход успешен (свой бюджет, doc@ не задет).
    const ok = await request.post(`${BASE}/api/v1/auth/login`, {
      data: { email: doc.email, password: DEMO_PASSWORD, otp: totp(doc.totpSecret) },
    });
    expect(ok.status()).toBe(200);
  });

  test("API: пациент входит без 2FA", async ({ request }) => {
    const r = await request.post(`${BASE}/api/v1/auth/login`, {
      data: { email: "p1@compass.demo", password: DEMO_PASSWORD },
    });
    expect(r.status()).toBe(200);
  });

  test("UI: врач видит ошибку без OTP, после ввода корректного кода входит", async ({ page, request }) => {
    const doc = await freshClinician(request);  // свой бюджет неудач
    await page.goto("/");
    await page.getByLabel("Email").fill(doc.email);
    await page.getByLabel("Пароль").fill(DEMO_PASSWORD);
    await page.getByRole("button", { name: "Войти" }).click();
    await expect(page.getByText("Требуется корректный код 2FA")).toBeVisible();

    // Остаёмся на форме — вводим TOTP и входим.
    await page.getByLabel("Код 2FA").fill(totp(doc.totpSecret));
    await page.getByRole("button", { name: "Войти" }).click();
    await expect(page.getByRole("button", { name: "Меню", exact: true })).toBeVisible();
  });

  test("UI: транзиентная 500 на /auth/config не кэшируется — второй логин без 2FA", async ({ page }) => {
    // Первые два ответа конфига (App + хелпер login) падают: ошибка не должна
    // залипать на весь прогон — иначе следующий логин ждёт отсутствующее поле.
    let configCalls = 0;
    await page.route("**/api/v1/auth/config", async (route) => {
      configCalls += 1;
      if (configCalls <= 2) {
        await route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ require_otp: false, mail: false }),
      });
    });
    // Вход не трогает настоящий API: проверяем тело запроса, который шлёт UI.
    const logins: Record<string, unknown>[] = [];
    await page.route("**/api/v1/auth/login", async (route) => {
      logins.push((route.request().postDataJSON() ?? {}) as Record<string, unknown>);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          access_token: "e2e-token",
          refresh_token: "e2e-refresh",
          role: "CLINICIAN",
        }),
      });
    });

    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    expect(logins).toHaveLength(1);

    // Чистая сессия — второй вход с той же страницы.
    await page.evaluate(() => localStorage.clear());
    const started = Date.now();
    await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
    const elapsed = Date.now() - started;

    // Ошибка не закэширована: конфиг спрошен заново. Запрос App уходит
    // после клика — досчитываем его поллингом, а не мгновенной проверкой.
    await expect.poll(() => configCalls, { timeout: 5000 }).toBe(4);
    // Второй вход по ветке «2FA выключен»: без otp и без ожидания поля.
    expect(logins[1]).not.toHaveProperty("otp");
    expect(elapsed).toBeLessThan(2500);
  });
});