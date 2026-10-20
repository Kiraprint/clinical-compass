import type { CapacitorConfig } from "@capacitor/cli";

/**
 * Capacitor-обёртка веб-клиента в нативное Android-приложение (item 4).
 *
 * webDir — собранный SPA (ui/dist). Сборка веб-части происходит в CI перед `cap sync`.
 * API-хост нативной сборки задаётся на этапе сборки через VITE_API_BASE
 * (см. ui/src/api.ts и .github/workflows/release-android.yml).
 */
const config: CapacitorConfig = {
  appId: "dev.compass.assistant",
  appName: "Compass",
  webDir: "../ui/dist",
  android: {
    allowMixedContent: false,
  },
  plugins: {
    PushNotifications: {
      presentationOptions: ["badge", "sound", "alert"],
    },
  },
};

export default config;
