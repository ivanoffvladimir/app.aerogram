/**
 * HTTP-клиент кабинета.
 *
 * Типы запросов и ответов генерируются из OpenAPI-схемы бэкенда (`pnpm generate:api`),
 * чтобы фронт не расходился с бэкендом молча. Ручные типы здесь — временные,
 * до появления первой сгенерированной схемы.
 */

export const API_PREFIX = '/api/v1'

/** Единый формат ошибки API (FR-10.5). */
export interface ApiErrorBody {
  code: string
  message: string
  field: string | null
  carrier_code: string | null
  request_id: string
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: ApiErrorBody,
  ) {
    super(body.message)
    this.name = 'ApiError'
  }
}

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface UserProfile {
  id: string
  tenant_id: string
  email: string
  full_name: string
  role: string
  is_active: boolean
  mfa_enabled: boolean
  last_login_at: string | null
}

type TokenReader = () => string | null

let readToken: TokenReader = () => null

/** Подключить источник токена. Вызывается один раз при инициализации хранилища. */
export function setTokenReader(reader: TokenReader): void {
  readToken = reader
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = readToken()
  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const response = await fetch(`${API_PREFIX}${path}`, { ...init, headers })

  if (!response.ok) {
    let body: ApiErrorBody
    try {
      body = ((await response.json()) as { error: ApiErrorBody }).error
    } catch {
      // Ответ без тела (например, от прокси) не должен ронять интерфейс.
      body = {
        code: 'unknown_error',
        message: 'Сервис недоступен, попробуйте позже',
        field: null,
        carrier_code: null,
        request_id: response.headers.get('X-Request-Id') ?? '-',
      }
    }
    throw new ApiError(response.status, body)
  }

  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

export const api = {
  login: (email: string, password: string, mfaCode?: string) =>
    request<TokenPair>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password, mfa_code: mfaCode ?? null }),
    }),

  refresh: (refreshToken: string) =>
    request<TokenPair>('/auth/refresh', {
      method: 'POST',
      body: JSON.stringify({ refresh_token: refreshToken }),
    }),

  me: () => request<UserProfile>('/auth/me'),

  users: () => request<UserProfile[]>('/users'),
}
