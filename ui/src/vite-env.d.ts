/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Базовый URL API для нативной сборки (Capacitor). Пусто = same-origin (веб). */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}