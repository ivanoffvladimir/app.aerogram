import { describe, expect, it } from 'vitest'
import { changedFields } from './patch'

describe('changedFields', () => {
  it('шлёт только изменённое', () => {
    const original = { name: 'Роспломба', phone: '+79161234567' }
    const patch = changedFields(original, { name: 'Роспломба-Юг', phone: '+79161234567' })
    expect(patch).toEqual({ name: 'Роспломба-Юг' })
  })

  it('ничего не меняли — тела нет вовсе', () => {
    // Пустой запрос не должен уходить: он трогает updated_at и аудит
    // без единой правки.
    const same = { name: 'Роспломба', phone: null }
    expect(changedFields(same, { ...same })).toEqual({})
  })

  it('стёртое поле уходит как null — это очистка', () => {
    const patch = changedFields({ phone: '+79161234567' }, { phone: '' })
    expect(patch).toEqual({ phone: null })
  })

  it('пробелы по краям правкой не считаются', () => {
    // Иначе случайный пробел при копировании отправлял бы запрос впустую.
    expect(changedFields({ name: 'Роспломба' }, { name: '  Роспломба  ' })).toEqual({})
  })

  it('пустое и отсутствующее — одно и то же', () => {
    expect(changedFields({ phone: null }, { phone: '   ' })).toEqual({})
  })

  it('переключатель меняется как есть', () => {
    expect(changedFields({ is_default_sender: false }, { is_default_sender: true })).toEqual({
      is_default_sender: true,
    })
  })
})
