import type { Money } from '@/api/client'

/** Число знаков в минорной единице. Совпадает с `shared/money.py` на бэкенде. */
const MINOR_UNIT_EXPONENTS: Record<string, number> = {
  JPY: 0,
  KRW: 0,
  CLP: 0,
  VND: 0,
  ISK: 0,
  BHD: 3,
  KWD: 3,
  OMR: 3,
  TND: 3,
}

/**
 * Деньги приходят целым числом минорных единиц (фронт-ТЗ, раздел 10).
 * Арифметика с плавающей точкой над ними запрещена; здесь только
 * форматирование, и `amount_minor` делится один раз, в последний момент.
 */
export function formatMoney(money: Money): string {
  const exponent = MINOR_UNIT_EXPONENTS[money.currency] ?? 2
  const value = money.amount_minor / 10 ** exponent
  return new Intl.NumberFormat('ru-RU', {
    style: 'currency',
    currency: money.currency,
    minimumFractionDigits: exponent,
    maximumFractionDigits: exponent,
  }).format(value)
}

/** Момент в таймзоне пользователя. Все метки времени приходят с зоной. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

/** Запас или опоздание в человеческом виде: секунды оператору не говорят ничего. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const hours = Math.floor(seconds / 3600)
  if (hours >= 24) {
    const days = Math.floor(hours / 24)
    return `${days} ${plural(days, 'сутки', 'суток', 'суток')}`
  }
  if (hours >= 1) return `${hours} ${plural(hours, 'час', 'часа', 'часов')}`
  return 'меньше часа'
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${Math.round(value * 100)}%`
}

function plural(count: number, one: string, few: string, many: string): string {
  const mod100 = count % 100
  if (mod100 >= 11 && mod100 <= 14) return many
  const mod10 = count % 10
  if (mod10 === 1) return one
  if (mod10 >= 2 && mod10 <= 4) return few
  return many
}

export const RISK_LABELS: Record<string, string> = {
  low: 'низкий',
  medium: 'средний',
  high: 'высокий',
}

export const CONFIDENCE_LABELS: Record<string, string> = {
  low: 'низкая',
  medium: 'средняя',
  high: 'высокая',
}

export const SOURCE_LABELS: Record<string, string> = {
  client_contract: 'Договор клиента',
  logistics_os: 'Тариф Logistics OS',
}

export const COST_COMPONENT_LABELS: Record<string, string> = {
  base: 'Базовый тариф',
  insurance: 'Страхование',
  pickup: 'Забор груза',
  door_delivery: 'Доставка до двери',
  packaging: 'Упаковка',
  remote_area: 'Удалённая зона',
  pallet: 'Паллетирование',
  waiting: 'Простой',
  declared_value: 'Объявленная ценность',
  other: 'Прочее',
}

export const INELIGIBILITY_LABELS: Record<string, string> = {
  misses_deadline: 'Не укладывается в срок',
  carrier_blacklisted: 'Перевозчик исключён',
  not_in_whitelist: 'Перевозчик не разрешён',
  service_unavailable: 'Услуга недоступна',
  cargo_restricted: 'Ограничение по грузу',
  tenant_policy: 'Запрещено политикой компании',
}
