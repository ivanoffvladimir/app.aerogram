import { defineConfig, devices } from '@playwright/test'

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
  // E2E идут против продакшн-сборки, а не против `next dev`. Причина не
  // в скорости: dev-сервер пересобирает маршруты на лету и после правок
  // файлов уходит в «__webpack_modules__ is not a function», из-за чего
  // страница перестаёт гидрироваться. Проверять нужно то, что отгружается.
  webServer: {
    command: 'pnpm build:server && pnpm start',
    url: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // Окружения, где браузер уже установлен и его версия не совпадает
        // с ожидаемой Playwright, задают путь через PLAYWRIGHT_CHROMIUM_PATH.
        // Скачивать браузер на месте нельзя, а падать из-за несовпадения
        // номера сборки — значит не запускать E2E вовсе.
        ...(process.env.PLAYWRIGHT_CHROMIUM_PATH
          ? { launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH } }
          : {}),
      },
    },
  ],
})
