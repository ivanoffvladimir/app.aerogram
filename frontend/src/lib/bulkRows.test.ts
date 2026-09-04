import { describe, expect, it } from 'vitest'
import { describeAddress } from './bulkRows'

describe('describeAddress', () => {
  it('склеивает город и адрес', () => {
    expect(describeAddress({ city: 'Москва', address_line: 'ул. Ленина, 1' })).toBe(
      'Москва, ул. Ленина, 1',
    )
  })

  it('переживает снимок другой формы, не роняя реестр', () => {
    // Снимок неизменяем: строка, сохранённая старой версией, останется
    // в базе навсегда, и весь реестр из-за неё падать не должен.
    expect(describeAddress({})).toBe('адрес не разобран')
    expect(describeAddress({ city: 42 } as unknown as Record<string, unknown>)).toBe(
      'адрес не разобран',
    )
  })

  it('показывает то, что есть, если поле одно', () => {
    expect(describeAddress({ city: 'Москва' })).toBe('Москва')
  })
})
