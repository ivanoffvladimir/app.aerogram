/**
 * Показ строк массового расчёта.
 *
 * Разбора списка здесь больше нет: его делает сервер (`POST /bulk-runs/import`),
 * один на всех клиентов, включая машинных. Живёт отдельно от экранов:
 * страница Next.js не имеет права экспортировать ничего, кроме самой
 * страницы, — а эти функции нужны тестам поштучно.
 */

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
