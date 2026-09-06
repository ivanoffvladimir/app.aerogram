import { describe, expect, it } from 'vitest'
import { describeAction, describeConditions } from './routingRules'

describe('describeAction', () => {
  it('называет действие по-русски', () => {
    expect(describeAction({ deny: true })).toBe('Запретить')
    expect(describeAction({ require_insurance: true })).toBe('Обязательное страхование')
  })

  it('у автовыбора показывает и правило выбора', () => {
    expect(describeAction({ auto_select: 'cheapest' })).toBe('Автовыбор: самый дешёвый')
  })

  it('незнакомое правило выбора показывает как есть', () => {
    // Перечисление на бэкенде может пополниться раньше словаря здесь.
    // Показать код честнее, чем показать пустоту.
    expect(describeAction({ auto_select: 'greenest' })).toBe('Автовыбор: greenest')
  })

  it('не молчит о действии, которого не понял', () => {
    // Пустая ячейка читалась бы как «правило ничего не делает», а это
    // правило, которое мы не разобрали, — разные вещи.
    expect(describeAction({})).toBe('Действие не распознано')
  })
})

describe('describeConditions', () => {
  it('пустое условие называет вслух', () => {
    // «Запретить» без условий запрещает всех, и это должно быть видно.
    expect(describeConditions({})).toEqual(['любой запрос'])
  })

  it('вес показывает в килограммах, а не в граммах', () => {
    expect(describeConditions({ weight: { min_grams: 30_000 } })).toEqual([
      'расчётный вес от 30 кг',
    ])
  })

  it('обе границы веса читаются как диапазон', () => {
    expect(describeConditions({ weight: { min_grams: 1_000, max_grams: 30_000 } })).toEqual([
      'расчётный вес от 1 до 30 кг',
    ])
  })

  it('стоимость показывает с валютой', () => {
    // Сумма без валюты не существует: «500 000» в рублях и в тенге —
    // разные величины, и правило написано про одну из них.
    //
    // Разделитель разрядов задан явно неразрывным пробелом: именно его
    // ставит русская локаль, и обычный пробел здесь дал бы тест, который
    // расходится с экраном на невидимом символе.
    expect(
      describeConditions({ cargo_value: { min_minor: 50_000_000, currency: 'RUB' } }),
    ).toEqual(['стоимость от 500\u00a0000 RUB'])
  })

  it('каждое условие — отдельная строка, потому что они соединены И', () => {
    const lines = describeConditions({
      carrier: ['cdek', 'pecom'],
      weight: { min_grams: 30_000 },
      cargo_type: ['cargo'],
    })
    expect(lines).toEqual([
      'перевозчики: cdek, pecom',
      'расчётный вес от 30 кг',
      'тип груза: груз',
    ])
  })

  it('различает «только опасные» и «только неопасные»', () => {
    // `false` здесь — условие, а не отсутствие условия: правило про
    // неопасные грузы не должно выглядеть правилом про любые.
    expect(describeConditions({ dangerous: true })).toEqual(['только опасные грузы'])
    expect(describeConditions({ dangerous: false })).toEqual(['только неопасные грузы'])
  })

  it('направление считает города, а не печатает их идентификаторы', () => {
    expect(
      describeConditions({ direction: { to: ['0c5b2444-70a0-4932-980c-b4dc0d3f02b5'] } }),
    ).toEqual(['направление: в 1 городов'])
  })
})
