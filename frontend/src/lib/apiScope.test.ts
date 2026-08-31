import { describe, expect, it } from 'vitest'
import { API_SCOPES, scopeLabel } from './apiScope'

describe('области API-ключа', () => {
  it('подписаны по-русски: владелец выбирает права, а не читает наш код', () => {
    expect(scopeLabel('shipments:write')).toBe('Создание и отмена отправлений')
  })

  it('неизвестная область показывается кодом, а не пропадает', () => {
    // Пропавшая строка означала бы, что ключ выглядит уже, чем он есть.
    expect(scopeLabel('counterparties:read')).toBe('counterparties:read')
  })

  it('словарь без дублей', () => {
    expect(new Set(API_SCOPES.map((s) => s.value)).size).toBe(API_SCOPES.length)
  })
})
