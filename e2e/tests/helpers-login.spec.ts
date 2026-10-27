// Проверки самого хелпера login() без сети и без App: page.request — это
// APIRequestContext, он НЕ проходит через page.route, поэтому подменяем page.
import { expect, test } from "@playwright/test";
import { DEMO_PASSWORD, DEMO_TOTP, login, resetOtpCache } from "./helpers";

type ConfigAnswer = { status: number; body?: unknown };

/** Фейковый page: считает запросы хелпера и заполнения полей формы. */
function fakePage(script: ConfigAnswer[]) {
  const calls = { config: 0, otpWaits: 0, otpFills: 0, submits: 0 };
  const field = (name: string) => ({
    fill: async () => {
      if (name === "Код 2FA") calls.otpFills += 1;
    },
    click: async () => {},
    waitFor: async () => {
      if (name !== "Код 2FA") return;
      calls.otpWaits += 1;
      throw new Error("поля «Код 2FA» нет"); // стенд без 2FA его не рисует
    },
  });
  const page = {
    url: () => "http://helper.test/",
    goto: async () => {},
    getByLabel: (name: string) => field(name),
    getByRole: (_role: string, opts?: { name?: string }) => {
      const loc = field(`role:${opts?.name ?? ""}`);
      return {
        ...loc,
        click: async () => {
          if (opts?.name === "Войти") calls.submits += 1;
        },
      };
    },
    request: {
      get: async (url: string) => {
        calls.config += 1;
        const answer = script[calls.config - 1] ?? script[script.length - 1];
        return {
          url: () => url,
          ok: () => answer.status === 200,
          json: async () => answer.body ?? {},
        };
      },
    },
  };
  return { page, calls };
}

test.beforeEach(() => resetOtpCache());

test("хелпер: транзиентная 500 не кэшируется, успешный «2FA выключен» — кэшируется", async () => {
  const { page, calls } = fakePage([{ status: 500 }, { status: 200, body: { require_otp: false } }]);

  // Конфиг упал: поле 2FA не найдено, но и вход не заблокирован.
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  expect(calls.config).toBe(1);
  expect(calls.otpWaits).toBe(1);
  expect(calls.otpFills).toBe(0);
  expect(calls.submits).toBe(1);

  // Ошибка не закэширована — хелпер спрашивает конфиг заново и сразу берёт
  // ветку «2FA выключен», без ожидания отсутствующего поля.
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  expect(calls.config).toBe(2);
  expect(calls.otpWaits).toBe(1);
  expect(calls.otpFills).toBe(0);

  // Успешный ответ кэширован: третий логин не ходит в /auth/config.
  await login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP);
  expect(calls.config).toBe(2);
  expect(calls.otpWaits).toBe(1);
});

test("хелпер: при require_otp=true пропавшее поле 2FA — ошибка, форму не отправляем", async () => {
  const { page, calls } = fakePage([{ status: 200, body: { require_otp: true } }]);
  await expect(login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP)).rejects.toThrow(
    /Код 2FA/,
  );
  expect(calls.otpWaits).toBe(1);
  expect(calls.otpFills).toBe(0);
  expect(calls.submits).toBe(0);
});

test("хелпер: require_otp=true не кэшируется", async () => {
  const { page, calls } = fakePage([{ status: 200, body: { require_otp: true } }]);
  await expect(login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP)).rejects.toThrow();
  await expect(login(page, "doc@compass.demo", DEMO_PASSWORD, DEMO_TOTP)).rejects.toThrow();
  expect(calls.config).toBe(2);
});
