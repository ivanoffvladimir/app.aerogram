import type { components } from './schema'

/**
 * Клиент API. Типы приходят из `docs/tz/v3/openapi.yaml` через
 * `pnpm generate:api` — руками их править нельзя, расхождение с контрактом
 * должно быть ошибкой сборки, а не находкой на демонстрации.
 */

export type Money = components['schemas']['Money']
export type Address = components['schemas']['Address']
export type Package = components['schemas']['Package']
export type RateRequest = components['schemas']['RateRequest']
export type RateResponse = components['schemas']['RateResponse']
export type RateOffer = components['schemas']['RateOffer']
export type CostComponent = components['schemas']['CostComponent']
export type CarrierFailure = components['schemas']['CarrierFailure']
/**
 * Рекомендация. Два поля расходятся со схемой контракта, и оба расхождения
 * идут от прозы того же ТЗ — они записаны в docs/status.md:
 *
 * - `confidence` в схеме отсутствует, но системное ТЗ, раздел 9, требует
 *   явно показывать низкую уверенность, а фронт-ТЗ, раздел 4, помещает её
 *   в карточку рекомендации;
 * - `recommended_offer_id` в схеме обязателен и не допускает null, но когда
 *   в срок не укладывается никто, рекомендации нет. Подставлять вместо неё
 *   первый попавшийся вариант значило бы выдать нарушение срока за совет.
 */
export type Recommendation = Omit<
  components['schemas']['Recommendation'],
  'recommended_offer_id'
> & {
  recommended_offer_id: string | null
  confidence?: 'low' | 'medium' | 'high'
}
export type RoutingRequest = components['schemas']['RoutingRequest']
export type DecisionRequest = components['schemas']['DecisionRequest']
export type DecisionResponse = components['schemas']['DecisionResponse']
export type AuthResponse = components['schemas']['AuthResponse']

/** Единый формат ошибки бэкенда. */
export interface ApiErrorBody {
  error: {
    code: string
    message: string
    field: string | null
    carrier_code: string | null
    request_id: string | null
  }
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly field: string | null
  /** Показывается в технических подробностях — по нему поддержка найдёт запрос. */
  readonly requestId: string | null

  constructor(status: number, body: Partial<ApiErrorBody>) {
    const error = body.error
    super(error?.message ?? 'Не удалось выполнить запрос')
    this.name = 'ApiError'
    this.status = status
    this.code = error?.code ?? 'unknown'
    this.field = error?.field ?? null
    this.requestId = error?.request_id ?? null
  }
}

const ACCESS_TOKEN_KEY = 'aerogram.access_token'
const REFRESH_TOKEN_KEY = 'aerogram.refresh_token'

export const tokens = {
  access: (): string | null => safeRead(ACCESS_TOKEN_KEY),
  refresh: (): string | null => safeRead(REFRESH_TOKEN_KEY),
  save(auth: AuthResponse): void {
    safeWrite(ACCESS_TOKEN_KEY, auth.access_token)
    if (auth.refresh_token) safeWrite(REFRESH_TOKEN_KEY, auth.refresh_token)
  },
  clear(): void {
    safeRemove(ACCESS_TOKEN_KEY)
    safeRemove(REFRESH_TOKEN_KEY)
  },
}

/**
 * Хранилище может быть недоступно: приватное окно, запрет сайту хранить
 * данные, серверный рендеринг. Падать из-за этого нельзя — кабинет
 * просто попросит войти заново.
 */
function safeRead(key: string): string | null {
  try {
    return globalThis.localStorage?.getItem(key) ?? null
  } catch {
    return null
  }
}

function safeWrite(key: string, value: string): void {
  try {
    globalThis.localStorage?.setItem(key, value)
  } catch {
    /* работаем без запоминания сессии */
  }
}

function safeRemove(key: string): void {
  try {
    globalThis.localStorage?.removeItem(key)
  } catch {
    /* нечего удалять */
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  /** Обязателен для решений и отправлений (бэкенд-ТЗ, раздел 6). */
  idempotencyKey?: string
  signal?: AbortSignal
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = tokens.access()
  if (token) headers.Authorization = `Bearer ${token}`
  if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey

  const response = await fetch(`/v1${path}`, {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    ...(options.signal ? { signal: options.signal } : {}),
  })

  if (!response.ok) {
    throw new ApiError(response.status, await readErrorBody(response))
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

async function readErrorBody(response: Response): Promise<Partial<ApiErrorBody>> {
  try {
    return (await response.json()) as Partial<ApiErrorBody>
  } catch {
    // Тело может быть пустым или не-JSON — например, при обрыве соединения.
    return {}
  }
}

/** Ключ идемпотентности на попытку, а не на повтор: повтор обязан его сохранить. */
export function newIdempotencyKey(): string {
  return globalThis.crypto.randomUUID()
}
