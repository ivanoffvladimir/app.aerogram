import { describe, expect, it } from 'vitest'
import {
  MAX_CITY_LOOKUP,
  citiesInRules,
  describeAction,
  describeConditions,
  pluralCities,
} from './routingRules'

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

  it('без известных названий показывает счёт, а не идентификаторы', () => {
    // Идентификатор ФИАС человеку не говорит ничего, а строка правила
    // должна оставаться читаемой и до того, как названия подгрузятся.
    expect(
      describeConditions({ direction: { to: ['0c5b2444-70a0-4932-980c-b4dc0d3f02b5'] } }),
    ).toEqual(['направление: куда 1 город'])
  })

  it('с названиями показывает города, а не счёт', () => {
    // Правило, направление которого читается как «2 города», нельзя
    // ни проверить, ни исправить.
    const names = new Map([
      ['a', 'Москва'],
      ['b', 'Владивосток'],
    ])
    expect(describeConditions({ direction: { from: ['b'], to: ['a'] } }, names)).toEqual([
      'направление: откуда Владивосток; куда Москва',
    ])
  })

  it('частично известные названия не смешиваются со счётом', () => {
    // Половина списка названиями, половина идентификаторами читалась бы
    // как разные вещи в одном перечислении. Либо все, либо счёт.
    const names = new Map([['a', 'Москва']])
    expect(describeConditions({ direction: { to: ['a', 'b'] } }, names)).toEqual([
      'направление: куда 2 города',
    ])
  })
})

describe('pluralCities', () => {
  it('согласует число с существительным', () => {
    // «в 1 городов» — то, как это выглядело до появления названий.
    expect(pluralCities(1)).toBe('город')
    expect(pluralCities(2)).toBe('города')
    expect(pluralCities(5)).toBe('городов')
    expect(pluralCities(21)).toBe('город')
  })

  it('одиннадцать и его соседи — исключение, а не правило', () => {
    // 11..14 склоняются не как 1..4, и это единственное место, где
    // остаток от деления на десять даёт неверный ответ.
    expect(pluralCities(11)).toBe('городов')
    expect(pluralCities(12)).toBe('городов')
    expect(pluralCities(14)).toBe('городов')
    expect(pluralCities(111)).toBe('городов')
  })
})

describe('citiesInRules', () => {
  it('собирает города всех правил одним списком без повторов', () => {
    // Запрос на каждое правило означал бы десяток обращений там,
    // где хватает одного.
    const rules = [
      { conditions: { direction: { from: ['a'], to: ['b'] } } },
      { conditions: { direction: { to: ['b', 'c'] } } },
      { conditions: { carrier: ['cdek'] } },
    ]
    expect(citiesInRules(rules)).toEqual(['a', 'b', 'c'])
  })

  it('правило без направления городов не добавляет', () => {
    expect(citiesInRules([{ conditions: {} }])).toEqual([])
  })

  it('длинный список подрезается пределом пути, а не отправляется целиком', () => {
    // Превышение даёт 422, и тогда не пришло бы НИ ОДНОГО названия.
    // Лучше подрезать: остальные правила покажут счёт, как во время загрузки.
    const many = Array.from(
      { length: 80 },
      (_, index) => `city-${String(index).padStart(3, '0')}`,
    )
    expect(citiesInRules([{ conditions: { direction: { to: many } } }])).toHaveLength(
      MAX_CITY_LOOKUP,
    )
  })
})
