import { describe, expect, it } from 'vitest'
import { IdempotencyKeys } from './idempotency'

describe('IdempotencyKeys', () => {
  it('возвращает тот же ключ при повторе того же действия', () => {
    // Повтор после потерянного ответа обязан прислать прежний ключ,
    // иначе появится второе решение по той же рекомендации.
    const keys = new IdempotencyKeys()
    expect(keys.for('offer-1')).toBe(keys.for('offer-1'))
  })

  it('даёт разные ключи разным вариантам', () => {
    // Смена варианта — новое намерение оператора, а не повтор прежнего.
    const keys = new IdempotencyKeys()
    expect(keys.for('offer-1')).not.toBe(keys.for('offer-2'))
  })

  it('после сброса выдаёт новый ключ', () => {
    // Новый расчёт — другие предложения: старый ключ вернул бы старое решение.
    const keys = new IdempotencyKeys()
    const before = keys.for('offer-1')
    keys.clear()
    expect(keys.for('offer-1')).not.toBe(before)
  })

  it('ключ похож на UUID, а не на счётчик', () => {
    const key = new IdempotencyKeys().for('offer-1')
    expect(key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
  })
})
