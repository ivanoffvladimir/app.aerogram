import { describe, expect, it } from 'vitest'
import { formatDuration, formatMoney, formatPercent } from './format'

describe('formatMoney', () => {
  it('делит минорные единицы один раз, в момент показа', () => {
    // 245050 копеек это 2450,50 ₽. Неразрывный пробел — от Intl.
    expect(formatMoney({ amount_minor: 245_050, currency: 'RUB' })).toMatch(/2\s?450,50/)
  })

  it('учитывает валюту без минорной единицы', () => {
    // У иены нет копеек: 241 это 241, а не 2,41.
    expect(formatMoney({ amount_minor: 241, currency: 'JPY' })).toMatch(/241/)
  })

  it('показывает ноль, а не пустую строку', () => {
    expect(formatMoney({ amount_minor: 0, currency: 'RUB' })).toMatch(/0,00/)
  })
})

describe('formatDuration', () => {
  it('склоняет часы по-русски', () => {
    expect(formatDuration(3600)).toBe('1 час')
    expect(formatDuration(3600 * 2)).toBe('2 часа')
    expect(formatDuration(3600 * 5)).toBe('5 часов')
    expect(formatDuration(3600 * 11)).toBe('11 часов')
  })

  it('переходит к суткам, когда часов становится много', () => {
    expect(formatDuration(3600 * 48)).toBe('2 суток')
  })

  it('не притворяется точным на малых значениях', () => {
    expect(formatDuration(120)).toBe('меньше часа')
  })

  it('отсутствие значения показывает прочерком, а не нулём', () => {
    // Ноль часов и «неизвестно» — разные вещи для оператора.
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(0)).toBe('меньше часа')
  })
})

describe('formatPercent', () => {
  it('переводит долю в проценты', () => {
    expect(formatPercent(0.97)).toBe('97%')
  })

  it('отсутствие данных не превращается в ноль процентов', () => {
    expect(formatPercent(null)).toBe('—')
  })
})

describe('подписи доверия', () => {
  it('переводит все значения, включая «данных не хватает»', async () => {
    const { CONFIDENCE_LABELS } = await import('./format')
    for (const level of ['low', 'medium', 'high', 'insufficient']) {
      expect(CONFIDENCE_LABELS[level], level).toMatch(/[А-Яа-я]/)
    }
  })
})
