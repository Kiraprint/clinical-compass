import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "tests",
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: process.env.CC_E2E_URL ?? "http://127.0.0.1:8090",
    headless: true,
  },
  reporter: [["list"]],
});