import { describe, expect, it } from 'vitest'
import { describeAddress, parseRecipients } from './bulkRows'

describe('parseRecipients', () => {
  it('разбирает строку «город; адрес»', () => {
    const { rows, errors } = parseRecipients('Москва; ул. Получателя, 10')
    expect(errors).toEqual([])
    expect(rows).toEqual([{ city: 'Москва', addressLine: 'ул. Получателя, 10' }])
  })

  it('оставляет точку с запятой внутри адреса адресу', () => {
    // Разделитель — первый, а не любой: иначе адрес молча обрежется.
    const { rows } = parseRecipients('Москва; ул. Ленина, 1; кв. 5')
    expect(rows[0]?.addressLine).toBe('ул. Ленина, 1; кв. 5')
  })

  it('пропускает пустые строки молча', () => {
    const { rows, errors } = parseRecipients('\nМосква; ул. Ленина, 1\n\n')
    expect(rows).toHaveLength(1)
    expect(errors).toEqual([])
  })

  it('отказывается разбирать строку без адреса', () => {
    // Молча выбросить получателя из рассылки хуже, чем отказаться
    // считать весь список: оператор не узнает о пропаже.
    const { rows, errors } = parseRecipients('Москва')
    expect(rows).toEqual([])
    expect(errors).toHaveLength(1)
    expect(errors[0]).toContain('1')
  })

  it('отказывается разбирать строку без города', () => {
    const { rows, errors } = parseRecipients('; ул. Ленина, 1')
    expect(rows).toEqual([])
    expect(errors).toHaveLength(1)
  })

  it('называет номер строки, считая и пустые', () => {
    // Оператор ищет ошибку глазами в своём же тексте: номер должен
    // совпадать с тем, что показывает его редактор.
    const { errors } = parseRecipients('Москва; ул. Ленина, 1\n\nВладивосток')
    expect(errors[0]).toContain('Строка 3')
  })
})

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
