/** Клиент API: единый формат ошибок и подстановка токена. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api, setTokenReader } from '../client'

const originalFetch = globalThis.fetch

function mockResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
}

describe('клиент API', () => {
  beforeEach(() => {
    setTokenReader(() => null)
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  it('возвращает разобранное тело успешного ответа', async () => {
    globalThis.fetch = vi.fn(async () =>
      mockResponse({ access_token: 'a', refresh_token: 'r', token_type: 'bearer', expires_in: 1800 }),
    ) as unknown as typeof fetch

    const tokens = await api.login('user@example.com', 'пароль')
    expect(tokens.access_token).toBe('a')
  })

  it('подставляет токен в заголовок Authorization', async () => {
    const spy = vi.fn<typeof fetch>(async () => mockResponse({ id: '1' }))
    globalThis.fetch = spy
    setTokenReader(() => 'eyJhbGciOiJIUzI1NiJ9.test')

    await api.me()

    const headers = spy.mock.calls[0]?.[1]?.headers as Headers
    expect(headers.get('Authorization')).toBe('Bearer eyJhbGciOiJIUzI1NiJ9.test')
  })

  it('не подставляет заголовок, когда токена нет', async () => {
    const spy = vi.fn<typeof fetch>(async () => mockResponse({ id: '1' }))
    globalThis.fetch = spy

    await api.me()

    const headers = spy.mock.calls[0]?.[1]?.headers as Headers
    expect(headers.has('Authorization')).toBe(false)
  })

  it('превращает ошибку API в ApiError с русским текстом', async () => {
    globalThis.fetch = vi.fn(async () =>
      mockResponse(
        {
          error: {
            code: 'unauthenticated',
            message: 'Неверный e-mail или пароль',
            field: null,
            carrier_code: null,
            request_id: 'rq_1',
          },
        },
        { status: 401 },
      ),
    ) as unknown as typeof fetch

    await expect(api.login('user@example.com', 'неверный')).rejects.toThrow(ApiError)
    await expect(api.login('user@example.com', 'неверный')).rejects.toThrow(
      'Неверный e-mail или пароль',
    )
  })

  it('переживает ответ без тела ошибки', async () => {
    // Прокси и балансировщик отвечают без JSON — интерфейс не должен падать.
    globalThis.fetch = vi.fn(
      async () => new Response('502 Bad Gateway', { status: 502 }),
    ) as unknown as typeof fetch

    await expect(api.me()).rejects.toThrow('Сервис недоступен, попробуйте позже')
  })
})
