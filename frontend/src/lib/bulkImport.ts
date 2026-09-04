/**
 * Предпросмотр импорта: какие строки готовы войти в прогон.
 *
 * Разбор и подбор по адресной книге делает сервер (`POST /bulk-runs/import`);
 * здесь — только решение оператора по строкам, где сервер выбрать не смог,
 * и сборка тела создания прогона. Живёт отдельно от экрана, чтобы это
 * решение проверялось на данных, без браузера.
 */

import type { Address, BulkImport, BulkImportRow, BulkImportStatus, Money } from '@/api/client'

export const IMPORT_STATUS_LABELS: Record<BulkImportStatus, string> = {
  parsed: 'Адрес из списка',
  resolved: 'Найден в адресной книге',
  ambiguous: 'Нужен выбор',
  not_found: 'Не найден',
}

/** Выбор оператора по неоднозначным строкам: номер строки → адрес. */
export type Choices = Record<number, string>

/** Общий груз прогона, применяемый к строкам без своего. */
export interface CommonCargo {
  weightGrams: number
  cargoValue: Money
}

export interface ReadyRow {
  line: number
  destination: Address
  weight_grams: number
  cargo_value: Money
}

/**
 * Адрес строки с учётом выбора оператора.
 *
 * `null` — строка не готова: не найдена, либо вариантов несколько и ни один
 * не выбран. Такая строка в прогон не попадает, и об этом говорится
 * счётчиком, а не молчанием: молча выбросить получателя из рассылки хуже,
 * чем отказаться.
 */
export function resolvedDestination(row: BulkImportRow, choices: Choices): Address | null {
  if (row.destination) return row.destination
  if (row.status !== 'ambiguous' || !row.match) return null
  const chosen = choices[row.line]
  if (!chosen) return null
  return row.match.options.find((option) => option.address_id === chosen)?.address ?? null
}

/** Строки, готовые к созданию прогона, и сколько строк в него не войдёт. */
export function readyRows(
  preview: BulkImport,
  choices: Choices,
  cargo: CommonCargo,
): { rows: ReadyRow[]; excluded: number } {
  const rows: ReadyRow[] = []
  let excluded = 0
  for (const row of preview.rows) {
    const destination = resolvedDestination(row, choices)
    if (!destination) {
      excluded += 1
      continue
    }
    rows.push({
      line: row.line,
      destination,
      // Груз строки из файла побеждает общий: файл называет его нарочно.
      weight_grams: row.weight_grams ?? cargo.weightGrams,
      cargo_value: row.cargo_value ?? cargo.cargoValue,
    })
  }
  return { rows, excluded }
}

/** Адрес одной строкой для таблицы предпросмотра. */
export function formatDestination(address: Address): string {
  return [address.postal_code, address.city, address.address_line]
    .filter((part): part is string => Boolean(part))
    .join(', ')
}
