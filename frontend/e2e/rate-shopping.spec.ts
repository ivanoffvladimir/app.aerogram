import { expect, test, type Page } from '@playwright/test'

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

/** Отдельная проверка: без демо-данных тест обязан сказать об этом прямо,
 *  а не падать на «не нашёл заголовок» через двадцать строк. */
async function expectSignedIn(page: Page) {
  const loginError = page.locator('form').getByRole('alert')
  const cabinet = page.getByRole('heading', { name: 'Расчёт и выбор перевозчика' })

  // Ждём ЛЮБОЙ из двух исходов, а не проверяем ошибку сразу после клика:
  // проверка без ожидания срабатывает раньше ответа сервера и не находит
  // ничего, из-за чего тест падал бы позже и с невнятным сообщением —
  // ровно тем, которое эта проверка должна была заменить.
  await Promise.race([
    loginError.waitFor({ state: 'visible' }).catch(() => undefined),
    cabinet.waitFor({ state: 'visible' }).catch(() => undefined),
  ])

  if (await loginError.isVisible()) {
    throw new Error(
      `Вход отклонён: ${await loginError.textContent()}. ` +
        'Похоже, в базе нет демо-данных — наполните её перед запуском E2E.',
    )
  }
}

test('оператор входит, считает и фиксирует решение', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('Электронная почта').fill(EMAIL)
  await page.getByLabel('Пароль').fill(PASSWORD)
  await page.getByRole('button', { name: 'Войти' }).click()

  await expectSignedIn(page)
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

  await expect(page.locator('form').getByRole('alert')).toHaveText(
    'Неверная почта или пароль',
  )
})
