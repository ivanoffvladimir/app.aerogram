import { describe, expect, it } from 'vitest'
import type { BulkImport, BulkImportRow } from '@/api/client'
import { formatDestination, readyRows, resolvedDestination } from './bulkImport'

const moscow = {
  country: 'RU',
  city: 'Москва',
  postal_code: '101000',
  address_line: 'ул. Ленина, 1',
}
const tver = { country: 'RU', city: 'Тверь', address_line: 'пр. Мира, 3' }

function row(overrides: Partial<BulkImportRow>): BulkImportRow {
  return {
    line: 1,
    status: 'parsed',
    message: null,
    lookup: null,
    match: null,
    destination: moscow,
    weight_grams: null,
    cargo_value: null,
    ...overrides,
  }
}

function preview(rows: BulkImportRow[]): BulkImport {
  return {
    rows,
    errors: [],
    counts: { parsed: 0, resolved: 0, ambiguous: 0, not_found: 0 },
    tabular: false,
  }
}

const cargo = { weightGrams: 1000, cargoValue: { amount_minor: 100_000, currency: 'RUB' } }

describe('resolvedDestination', () => {
  it('берёт адрес сервера, когда он есть', () => {
    expect(resolvedDestination(row({}), {})).toEqual(moscow)
  })

  it('неоднозначная строка без выбора не готова', () => {
    const ambiguous = row({
      status: 'ambiguous',
      destination: null,
      match: {
        counterparty_id: 'c1',
        counterparty_name: 'Роспломба',
        address_id: null,
        options: [
          { address_id: 'a1', address: moscow },
          { address_id: 'a2', address: tver },
        ],
      },
    })
    expect(resolvedDestination(ambiguous, {})).toBeNull()
    expect(resolvedDestination(ambiguous, { 1: 'a2' })).toEqual(tver)
  })

  it('выбор, которого нет среди вариантов, не адрес', () => {
    // Устаревший выбор после повторного разбора не должен подставить чужой адрес.
    const ambiguous = row({
      status: 'ambiguous',
      destination: null,
      match: {
        counterparty_id: 'c1',
        counterparty_name: 'Роспломба',
        address_id: null,
        options: [{ address_id: 'a1', address: moscow }],
      },
    })
    expect(resolvedDestination(ambiguous, { 1: 'zzz' })).toBeNull()
  })

  it('не найденная строка не готова, что бы ни выбрали', () => {
    expect(
      resolvedDestination(row({ status: 'not_found', destination: null }), { 1: 'a1' }),
    ).toBe(null)
  })
})

describe('readyRows', () => {
  it('считает не вошедшие строки, а не выбрасывает их молча', () => {
    const result = readyRows(
      preview([row({ line: 1 }), row({ line: 2, status: 'not_found', destination: null })]),
      {},
      cargo,
    )
    expect(result.rows.map((r) => r.line)).toEqual([1])
    expect(result.excluded).toBe(1)
  })

  it('груз строки из файла побеждает общий', () => {
    const result = readyRows(
      preview([
        row({
          line: 1,
          weight_grams: 1500,
          cargo_value: { amount_minor: 5000, currency: 'RUB' },
        }),
        row({ line: 2 }),
      ]),
      {},
      cargo,
    )
    expect(result.rows[0]).toMatchObject({
      weight_grams: 1500,
      cargo_value: { amount_minor: 5000 },
    })
    expect(result.rows[1]).toMatchObject({
      weight_grams: 1000,
      cargo_value: { amount_minor: 100_000 },
    })
  })
})

describe('formatDestination', () => {
  it('пропускает пустой индекс', () => {
    expect(formatDestination(tver)).toBe('Тверь, пр. Мира, 3')
    expect(formatDestination(moscow)).toBe('101000, Москва, ул. Ленина, 1')
  })
})
