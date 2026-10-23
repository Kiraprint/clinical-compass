import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "tests",
  timeout: 90_000,
  // Ответ ассистента включает вызов LLM (шлюз, свободные дорожки): 10–20 с —
  // норма, дефолтные 5 с на assertion давали ложные падения e2e.
  expect: { timeout: 30_000 },
  retries: 0,
  use: {
    baseURL: process.env.CC_E2E_URL ?? "http://127.0.0.1:8090",
    headless: true,
    // fxTunnel показывает одноразовую страницу-предупреждение на первый GET text/html
    // (12ч cookie после клика). Заголовок снимает её для e2e; на localhost он безвреден.
    extraHTTPHeaders: { "X-FxTunnel-Skip-Warning": "true" },
  },
  // Один демо-стек и одна БД на весь прогон: тесты меняют её состояние
  // (эскалации, напоминания, ротстер), поэтому параллельные воркеры ловят гонки.
  workers: 1,
  reporter: [["list"]],
});