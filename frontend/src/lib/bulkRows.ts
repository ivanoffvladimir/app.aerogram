/**
 * Разбор и показ строк массового расчёта.
 *
 * Живёт отдельно от экранов: страница Next.js не имеет права экспортировать
 * ничего, кроме самой страницы, — а эти функции нужны тестам поштучно.
 */

/** Разделитель полей строки списка: город, затем адрес. */
export const FIELD_SEPARATOR = ';'

export interface ParsedRecipient {
  city: string
  addressLine: string
}

/**
 * Разобрать список получателей из текста: одна строка — один получатель,
 * «город; адрес».
 *
 * Это временная форма ввода, и она названа временной в интерфейсе. Импорт
 * файла и подбор по адресной книге — следующая стадия (ADR-0022): делать
 * их до того, как оператор увидит работающий прогон, значит строить разбор
 * форматов вслепую.
 *
 * Пустые строки пропускаются молча, строка без адреса — нет: молча выбросить
 * получателя из рассылки хуже, чем отказаться считать весь список.
 */
export function parseRecipients(text: string): { rows: ParsedRecipient[]; errors: string[] } {
  const rows: ParsedRecipient[] = []
  const errors: string[] = []
  text.split('\n').forEach((line, index) => {
    const trimmed = line.trim()
    if (!trimmed) return
    // Пустая строка по умолчанию, а не `undefined`: со `split` первый элемент
    // есть всегда, но типы под `noUncheckedIndexedAccess` этого не знают.
    const [city = '', ...rest] = trimmed.split(FIELD_SEPARATOR)
    const addressLine = rest.join(FIELD_SEPARATOR).trim()
    if (!city.trim() || !addressLine) {
      errors.push(`Строка ${index + 1}: нужны город и адрес через «${FIELD_SEPARATOR}»`)
      return
    }
    rows.push({ city: city.trim(), addressLine })
  })
  return { rows, errors }
}

/**
 * Город и адрес из снимка получателя или отправителя.
 *
 * Снимок неизменяем и типизирован на сервере, но по проводу приходит
 * свободным объектом (`AddressSchema` в JSONB). Читаем его защищённо:
 * реестр на тысячу строк не должен падать целиком из-за одной строки
 * со снимком другой формы.
 */
export function describeAddress(snapshot: Record<string, unknown>): string {
  const city = typeof snapshot.city === 'string' ? snapshot.city : ''
  const line = typeof snapshot.address_line === 'string' ? snapshot.address_line : ''
  const parts = [city, line].filter(Boolean)
  return parts.length ? parts.join(', ') : 'адрес не разобран'
}
