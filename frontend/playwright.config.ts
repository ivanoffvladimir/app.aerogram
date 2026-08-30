import { defineConfig } from '@playwright/test'

/**
 * E2E по разделу 12 фронт-ТЗ. Сценарии проверяют путь оператора целиком,
 * поэтому им нужен поднятый бэкенд: без него они падают осмысленно,
 * а не показывают зелёный результат на моках.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:5173',
    trace: 'on-first-retry',
  },
})
