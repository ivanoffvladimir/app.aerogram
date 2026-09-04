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
export type TrackingEvent = components['schemas']['TrackingEvent']

/**
 * Отправление. Три поля добавлены к схеме контракта, и все три — то, без чего
 * экран не собирается:
 *
 * - `number` — внутренний номер. FR-2.4 требует его отдавать, и именно по нему
 *   идёт разговор с перевозчиком, но в схеме `Shipment` его нет;
 * - `carrier_name` — иначе в списке пришлось бы показывать UUID перевозчика;
 * - `created_at` — по нему список отсортирован, и без него нечего показать
 *   в колонке даты.
 *
 * Расхождение записано в docs/status.md.
 */
export type Shipment = components['schemas']['Shipment'] & {
  number: string
  carrier_name: string | null
  created_at: string
}

/**
 * Страница списка отправлений. Схемы ответа у `GET /v1/shipments` в контракте
 * нет вовсе — только `description: Shipment list`, — поэтому тип написан
 * руками по параметрам страницы из того же контракта.
 */
export interface ShipmentPage {
  items: Shipment[]
  total: number
  page: number
  page_size: number
}

/**
 * Сводка кабинета. Пути в контракте нет — экран /dashboard есть в ТЗ фронта,
 * а `openapi.yaml` заморожен как P0-набор, — поэтому тип написан руками
 * по `reports/schemas.py`.
 *
 * Доли приходят в процентах, а `null` означает «мерить было нечего»,
 * а не ноль.
 */
export interface Summary {
  days: number
  since: string
  delivery: {
    delivered: number
    with_deadline: number
    on_time: number
    late: number
    on_time_rate: number | null
    average_delay_hours: number | null
    max_delay_hours: number | null
    damaged: number
    claims: number
  }
  costs: {
    currency: string
    shipments: number
    quoted: Money
    actual: Money
    with_actual: number
  }[]
  overrides: {
    decisions: number
    overrides: number
    auto: number
    override_rate: number | null
    by_reason: Record<string, number>
  }
  exceptions: Record<string, number>
  exceptions_total: number
}

/**
 * Ключ машинного доступа. Значения ключа здесь нет и быть не может:
 * в базе лежит только хеш, и восстановить его нельзя (FR-10.2).
 */
export interface ApiKey {
  id: string
  name: string
  prefix: string
  scopes: string[]
  created_at: string
  last_used_at: string | null
  expires_at: string | null
}

/** Ответ на выпуск: `secret` показывается один раз и больше не возвращается. */
export interface ApiKeyCreated {
  key: ApiKey
  secret: string
}

/** Состояние массового расчёта. */
export type BulkRunStatus =
  | 'draft'
  | 'quoting'
  | 'quoted'
  | 'creating'
  | 'completed'
  | 'failed'

/** Состояние одной строки массового расчёта. */
export type BulkRowStatus = 'new' | 'quoted' | 'selected' | 'created' | 'failed'

/**
 * Строка массового расчёта: один получатель.
 *
 * Своих цифр строка не хранит — только ссылки на расчёт, рекомендацию,
 * решение и отправление (ADR-0022). Поэтому цену и срок экран берёт
 * по `rate_quote_id`, а не из самой строки.
 */
export interface BulkRow {
  id: string
  position: number
  status: BulkRowStatus
  error_message: string | null
  rate_quote_id: string | null
  recommendation_id: string | null
  decision_id: string | null
  shipment_id: string | null
  recipient_snapshot: Record<string, unknown>
  cargo_snapshot: Record<string, unknown>
}

/**
 * Массовый расчёт целиком. Пути в контракте ТЗ v3 нет — массовых отправлений
 * там не описано вовсе, решение принято отдельно (ADR-0021, ADR-0022),
 * поэтому тип написан руками по `bulk/schemas.py`.
 *
 * `counts` считается запросом на сервере: прогон бывает на тысячу строк,
 * и пересчитывать их на фронте ради счётчика незачем.
 */
export interface BulkRun {
  id: string
  name: string
  status: BulkRunStatus
  strategy: string | null
  sender_snapshot: Record<string, unknown>
  created_at: string
  updated_at: string
  rows: BulkRow[]
  counts: Record<string, number>
}

export interface BulkRunPage {
  items: BulkRun[]
  total: number
}

/**
 * Итог разбора одной строки импортированного списка (ADR-0022, стадия 2).
 *
 * `parsed` — адрес взят из самой строки; `resolved` — найден в адресной книге;
 * `ambiguous` — найдено несколько, выбирать оператору; `not_found` — искали,
 * не нашли. Типизировано, чтобы новое состояние на сервере ломало сборку,
 * а не показывалось оператору по-английски.
 */
export type BulkImportStatus = 'parsed' | 'resolved' | 'ambiguous' | 'not_found'

export interface BulkImportOption {
  address_id: string
  address: Address
}

export interface BulkImportMatch {
  counterparty_id: string
  counterparty_name: string
  address_id: string | null
  options: BulkImportOption[]
}

export interface BulkImportRow {
  line: number
  status: BulkImportStatus
  message: string | null
  lookup: string | null
  match: BulkImportMatch | null
  destination: Address | null
  weight_grams: number | null
  cargo_value: Money | null
}

/** Предпросмотр импорта: прогон ещё не создан. */
export interface BulkImport {
  rows: BulkImportRow[]
  errors: string[]
  counts: Record<BulkImportStatus, number>
  tabular: boolean
}

/** Причина, по которой отправление попало в разбор. */
export type ExceptionReason = 'deadline_passed' | 'problem_status' | 'stalled'

/** Строка разбора исключений. Причин может быть несколько сразу. */
export interface ShipmentException {
  id: string
  number: string
  carrier_name: string | null
  tracking_number: string | null
  // Словарь контракта, а не наш внутренний: сервер прогоняет статус через
  // `contract_status()`. Поэтому подписи экрана типизируются и незнакомое
  // значение ломает сборку, а не показывается оператору по-английски.
  status: Shipment['status']
  deadline: string | null
  last_event_at: string | null
  reasons: ExceptionReason[]
}

/**
 * Разбор исключений целиком. Пути в контракте нет — раздел 10 ТЗ требует вида
 * «что горит» поперёк отправлений, а `openapi.yaml` описывает только ленту
 * одного, — поэтому тип написан руками по `tracking/schemas.py`.
 *
 * `truncated` показывается пользователю: список ограничен сверху, и молчать
 * о том, что за пределом осталось непросмотренное, значит выдавать усечённый
 * список за полный.
 */
export interface ShipmentExceptionsPage {
  items: ShipmentException[]
  total: number
  scanned: number
  truncated: boolean
  by_reason: Record<ExceptionReason, number>
}

/**
 * Строка аналитики перевозчика. У `GET /v1/analytics/carriers` в контракте
 * схемы ответа тоже нет.
 *
 * `score === null` при `confidence === 'insufficient'` — это не ошибка,
 * а обязательное поведение (FR-7.3): показывается «недостаточно данных»,
 * а не число.
 */
export interface CarrierAnalytics {
  carrier_id: string
  carrier_code: string
  carrier_name: string
  score: number | null
  confidence: 'high' | 'medium' | 'low' | 'insufficient'
  scope_type: 'global' | 'direction' | 'direction_weight' | null
  scope_key: string
  sample_size: number
  period_start: string | null
  period_end: string | null
  components: {
    on_time_rate: number | null
    reliability: number | null
    incident_rate: number | null
    price_index: number | null
    data_quality: number | null
  }
  formula_version: string | null
  calculated_at: string | null
}

/**
 * Постраничная выдача ядра. Формат единый для всего API и отличается от
 * страницы отправлений: там `page`/`page_size`, здесь `limit`/`offset`.
 * Приведение их к одному виду — правка контракта, а не клиента.
 */
export interface CorePage<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

/**
 * Адрес контрагента.
 *
 * Ни адресной книги, ни пользователей в `docs/tz/v3/openapi.yaml` нет вовсе —
 * там описан только путь расчёта и оформления. Поэтому типы ниже написаны
 * руками по схемам `core/schemas.py`. Расхождение записано в docs/status.md;
 * когда пути появятся в контракте, эти определения должны исчезнуть в пользу
 * сгенерированных.
 */
export interface CounterpartyAddress {
  id: string
  counterparty_id: string
  label: string | null
  country_code: string
  region: string | null
  city: string
  postal_code: string | null
  street: string | null
  house: string | null
  flat: string | null
  is_default_sender: boolean
}

/** Контрагент адресной книги (FR-8.4). */
export interface Counterparty {
  id: string
  type: 'legal' | 'individual' | 'entrepreneur'
  name: string
  inn: string | null
  kpp: string | null
  contact_person: string | null
  phone: string | null
  email: string | null
  addresses: CounterpartyAddress[]
}

/**
 * Пользователь тенанта.
 *
 * `role` здесь — роль ХРАНЕНИЯ: в ней есть и платформенные значения, которых
 * владелец тенанта выдать не может. Роли, доступные для выдачи, перечислены
 * отдельно в `TENANT_ROLES`: список, из которого нельзя выбрать лишнего,
 * надёжнее проверки, которую можно забыть.
 */
export interface User {
  id: string
  tenant_id: string
  email: string
  full_name: string
  role: string
  is_active: boolean
  mfa_enabled: boolean
  last_login_at: string | null
}

/**
 * Перевозчик и состояние подключения тенанта (`GET /v1/carriers`).
 *
 * Путь в контракте объявлен без схемы ответа — только
 * `description: Carriers`, — поэтому тип написан руками по
 * `directories/schemas.py`.
 *
 * Учётных данных здесь нет и быть не может: приходят только имена полей,
 * которые нужно ввести для подключения.
 */
export interface CarrierConnection {
  carrier_id: string
  code: string
  name: string
  logo_url: string | null
  capabilities: Record<string, unknown>
  volumetric_divisor: number
  connected: boolean
  mode: 'own_contract' | 'aerogram' | null
  is_sandbox: boolean | null
  status: string | null
  status_message: string | null
  contract_number: string | null
  credential_fields: { name: string; label: string; secret: boolean; required: boolean }[]
  where_to_get: string | null
}

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
