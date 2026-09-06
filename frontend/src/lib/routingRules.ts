/**
 * Правило маршрутизации → строка, понятная логисту.
 *
 * Разбор живёт отдельно от экрана по той же причине, по какой отдельно
 * живёт `reconciliation.ts`: правило хранится в JSONB, и превратить его
 * в человеческую фразу — это правило, которое надо проверить тестом,
 * а не разметка, которую надо посмотреть глазами.
 *
 * Состав языка задан ADR-0028; здесь только его чтение. Проверять состав
 * фронт не пытается намеренно: вторая копия правил проверки однажды
 * разошлась бы с той, что стоит на бэкенде, и разошлась бы молча.
 */

/** Действия языка правил. Ровно одно на правило. */
export type RuleActionKind = 'deny' | 'allow' | 'require_insurance' | 'auto_select'

export const ACTION_LABELS: Record<RuleActionKind, string> = {
  deny: 'Запретить',
  allow: 'Только эти (whitelist)',
  require_insurance: 'Обязательное страхование',
  auto_select: 'Автовыбор',
}

/** Правила автовыбора — перечисление `SelectionRule` бэкенда. */
export const SELECTION_LABELS: Record<string, string> = {
  cheapest: 'самый дешёвый',
  fastest: 'самый быстрый',
  best_score: 'с лучшим Carrier Score',
  best_value: 'лучший по соотношению',
  cheapest_meeting_deadline: 'самый дешёвый в срок',
}

export const CARGO_TYPE_LABELS: Record<string, string> = {
  documents: 'документы',
  parcel: 'посылка',
  cargo: 'груз',
  equipment: 'оборудование',
}

/** Что делает правило — одной строкой. */
export function describeAction(actions: Record<string, unknown>): string {
  if (actions.deny === true) return ACTION_LABELS.deny
  if (actions.allow === true) return ACTION_LABELS.allow
  if (actions.require_insurance === true) return ACTION_LABELS.require_insurance
  if (typeof actions.auto_select === 'string') {
    const rule = SELECTION_LABELS[actions.auto_select] ?? actions.auto_select
    return `${ACTION_LABELS.auto_select}: ${rule}`
  }
  // Правило без известного действия сохранить нельзя, но показать его надо:
  // молча пустая ячейка выглядела бы правилом, которое ничего не делает,
  // а на самом деле это правило, которого мы не поняли.
  return 'Действие не распознано'
}

/**
 * Условия — списком строк, а не одной фразой.
 *
 * Условия соединяются логическим И, и «вес от 30 кг И только оборудование»
 * в одну строку читается хуже, чем два пункта: в сплошной фразе «и»
 * теряется, а от него зависит, срабатывает правило или нет.
 */
export function describeConditions(conditions: Record<string, unknown>): string[] {
  const parts: string[] = []

  const carrier = conditions.carrier
  if (Array.isArray(carrier)) parts.push(`перевозчики: ${carrier.join(', ')}`)

  const direction = conditions.direction
  if (direction && typeof direction === 'object') {
    const from = (direction as Record<string, unknown>).from
    const to = (direction as Record<string, unknown>).to
    const sides: string[] = []
    if (Array.isArray(from)) sides.push(`из ${from.length} городов`)
    if (Array.isArray(to)) sides.push(`в ${to.length} городов`)
    parts.push(`направление: ${sides.join(', ')}`)
  }

  const weight = conditions.weight
  if (weight && typeof weight === 'object') {
    const min = (weight as Record<string, unknown>).min_grams
    const max = (weight as Record<string, unknown>).max_grams
    parts.push(`расчётный вес ${range(gramsToKg(min), gramsToKg(max), 'кг')}`)
  }

  const value = conditions.cargo_value
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    const currency = typeof record.currency === 'string' ? record.currency : ''
    const min = minorToMajor(record.min_minor)
    const max = minorToMajor(record.max_minor)
    parts.push(`стоимость ${range(min, max, currency)}`)
  }

  const cargoType = conditions.cargo_type
  if (Array.isArray(cargoType)) {
    const names = cargoType.map((type) => CARGO_TYPE_LABELS[String(type)] ?? String(type))
    parts.push(`тип груза: ${names.join(', ')}`)
  }

  if (conditions.dangerous === true) parts.push('только опасные грузы')
  if (conditions.dangerous === false) parts.push('только неопасные грузы')

  // Пустое условие — законная запись, и молчать о ней нельзя: правило
  // «запретить» без условий запрещает всех, и это должно быть видно.
  return parts.length > 0 ? parts : ['любой запрос']
}

function gramsToKg(value: unknown): number | null {
  return typeof value === 'number' ? value / 1000 : null
}

function minorToMajor(value: unknown): number | null {
  return typeof value === 'number' ? value / 100 : null
}

/** «от 30 до 100 кг», «от 30 кг», «до 100 кг». */
function range(min: number | null, max: number | null, unit: string): string {
  const suffix = unit ? ` ${unit}` : ''
  if (min !== null && max !== null) return `от ${format(min)} до ${format(max)}${suffix}`
  if (min !== null) return `от ${format(min)}${suffix}`
  if (max !== null) return `до ${format(max)}${suffix}`
  return `без границ${suffix}`
}

function format(value: number): string {
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 }).format(value)
}
