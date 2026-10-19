// Refresh-token rotation (F4): каждый /auth/refresh отзывает старый jti;
// повторное использование отозванного токена → 401 TOKEN_EXPIRED.
import { expect, test, type APIResponse } from "@playwright/test";
import { DEMO_PASSWORD } from "./helpers";

const BASE = process.env.CC_E2E_URL ?? "http://127.0.0.1:8090";
const j = async (r: APIResponse) => r.json();

test.describe("Refresh-token rotation", () => {
  test("повторное использование старого refresh → 401, logout отзывает новый", async ({ request }) => {
    const login = await request.post(`${BASE}/api/v1/auth/login`, {
      data: { email: "p1@compass.demo", password: DEMO_PASSWORD },
    });
    expect(login.status()).toBe(200);
    const rt1 = (await j(login)).refresh_token as string;

    // Первый refresh: старый jti отзывается, выдаётся новый.
    const refresh1 = await request.post(`${BASE}/api/v1/auth/refresh`, {
      data: { refresh_token: rt1 },
    });
    expect(refresh1.status()).toBe(200);
    const body1 = await j(refresh1);
    expect(body1.refresh_token).not.toBe(rt1);

    // Повторное использование rt1 (уже отозван) → 401 TOKEN_EXPIRED.
    const reuse = await request.post(`${BASE}/api/v1/auth/refresh`, {
      data: { refresh_token: rt1 },
    });
    expect(reuse.status()).toBe(401);
    expect((await j(reuse)).code).toBe("TOKEN_EXPIRED");

    // Logout отзывает текущий refresh.
    const logout = await request.post(`${BASE}/api/v1/auth/logout`, {
      data: { refresh_token: body1.refresh_token },
    });
    expect(logout.status()).toBe(204);

    // После logout тот же токен больше не работает.
    const afterLogout = await request.post(`${BASE}/api/v1/auth/refresh`, {
      data: { refresh_token: body1.refresh_token },
    });
    expect(afterLogout.status()).toBe(401);
  });
});