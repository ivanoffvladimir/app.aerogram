import { describe, expect, it } from 'vitest'
import type { CurrencyTotals, Money } from '@/api/client'
import {
  STATE_LABELS,
  STATE_ORDER,
  differenceTone,
  hasAnythingToReconcile,
  reconciledCount,
} from '@/lib/reconciliation'

function money(amount_minor: number): Money {
  return { amount_minor, currency: 'RUB' }
}

function totals(overrides: Partial<CurrencyTotals> = {}): CurrencyTotals {
  return {
    currency: 'RUB',
    shipments: 0,
    quoted: money(0),
    quoted_reconciled: money(0),
    actual: money(0),
    difference: money(0),
    difference_percent: null,
    awaiting: 0,
    no_quote: 0,
    matched: 0,
    overcharged: 0,
    undercharged: 0,
    ...overrides,
  }
}

describe('differenceTone', () => {
  it('красит перерасход и недобор по-разному', () => {
    expect(differenceTone(money(500))).toBe('over')
    expect(differenceTone(money(-500))).toBe('under')
  })

  it('пустая разница выглядит как ноль, а не как экономия', () => {
    // Подкрасив «счёта нет» зелёным, экран сказал бы «сэкономили» там,
    // где счёт просто не пришёл.
    expect(differenceTone(null)).toBe('zero')
    expect(differenceTone(money(0))).toBe('zero')
  })
})

describe('reconciledCount', () => {
  it('считает только те строки, где было что сравнивать', () => {
    const row = totals({
      matched: 2,
      overcharged: 1,
      undercharged: 1,
      awaiting: 7,
      no_quote: 3,
    })
    expect(reconciledCount(row)).toBe(4)
  })

  it('ожидающие счёта не попадают в знаменатель', () => {
    expect(reconciledCount(totals({ awaiting: 10 }))).toBe(0)
  })
})

describe('hasAnythingToReconcile', () => {
  it('период без единого счёта — это не «всё сошлось»', () => {
    expect(hasAnythingToReconcile([totals({ shipments: 5, awaiting: 5 })])).toBe(false)
  })

  it('одна сверенная строка уже делает итог осмысленным', () => {
    expect(hasAnythingToReconcile([totals({ awaiting: 5 }), totals({ matched: 1 })])).toBe(true)
  })

  it('пустой период тоже нечего сверять', () => {
    expect(hasAnythingToReconcile([])).toBe(false)
  })
})

describe('подписи состояний', () => {
  it('каждое состояние фильтра имеет русскую подпись', () => {
    for (const state of STATE_ORDER) {
      expect(STATE_LABELS[state]).toBeTruthy()
    }
  })

  it('фильтр перечисляет все состояния, а не часть', () => {
    expect(new Set(STATE_ORDER)).toEqual(new Set(Object.keys(STATE_LABELS)))
  })
})
