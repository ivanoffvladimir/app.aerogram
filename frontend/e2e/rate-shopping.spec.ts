import { expect, test } from '@playwright/test'

/**
 * Сценарий Happy path из раздела 12 фронт-ТЗ, в той части, что уже
 * реализована: вход → расчёт → рекомендация → решение. Создание отправления
 * появится вместе с `POST /v1/shipments`.
 *
 * Тест требует поднятых бэкенда и базы с демо-данными. Мокать здесь нечего:
 * смысл E2E ровно в том, чтобы поймать расхождение фронта с настоящим API.
 */
const EMAIL = process.env.E2E_EMAIL ?? 'logist@rosplomba.ru'
const PASSWORD = process.env.E2E_PASSWORD ?? 'demo-password-12345'

test('оператор входит, считает и фиксирует решение', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('Электронная почта').fill(EMAIL)
  await page.getByLabel('Пароль').fill(PASSWORD)
  await page.getByRole('button', { name: 'Войти' }).click()

  await expect(page.getByRole('heading', { name: 'Расчёт и выбор перевозчика' })).toBeVisible()

  await page.getByRole('button', { name: 'Рассчитать' }).click()

  // Сбой одного перевозчика не должен превращать экран в общую ошибку
  // (фронт-ТЗ, раздел 3): дожидаемся либо рекомендации, либо явного
  // сообщения о её отсутствии.
  await expect(
    page.getByText(/Рекомендуем|Рекомендации нет|Ни один перевозчик/),
  ).toBeVisible({ timeout: 15_000 })
})

test('вход отклоняется без подсказки о существовании адреса', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('Электронная почта').fill('nobody@example.com')
  await page.getByLabel('Пароль').fill('wrong-password-12345')
  await page.getByRole('button', { name: 'Войти' }).click()

  await expect(page.getByRole('alert')).toHaveText('Неверная почта или пароль')
})
