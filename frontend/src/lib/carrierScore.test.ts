import { describe, expect, it } from 'vitest'
import type { CarrierAnalytics } from '@/api/client'
import { COMPONENTS, byScore, confidenceText, scoreText } from './carrierScore'

function row(overrides: Partial<CarrierAnalytics> = {}): CarrierAnalytics {
  return {
    carrier_id: 'id',
    carrier_code: 'cdek',
    carrier_name: 'СДЭК',
    score: 84,
    confidence: 'high',
    scope_type: 'global',
    scope_key: '',
    sample_size: 120,
    period_start: null,
    period_end: null,
    components: {
      on_time_rate: 0.94,
      reliability: 0.99,
      incident_rate: 0.02,
      price_index: 0.5,
      data_quality: 0.8,
    },
    formula_version: 'score-1.0.0',
    calculated_at: null,
    ...overrides,
  }
}

describe('scoreText', () => {
  it('без оценки показывает слова, а не ноль', () => {
    // Ноль читался бы как «перевозчик плохой», а пустое место — как поломка
    // экрана. Ни то, ни другое не значит «мы ещё не знаем».
    expect(scoreText(row({ score: null, confidence: 'insufficient' }))).toBe('нет оценки')
  })

  it('оценку показывает числом', () => {
    expect(scoreText(row({ score: 0 }))).toBe('0')
  })
})

describe('confidenceText', () => {
  it('называет размер выборки рядом с доверием', () => {
    expect(confidenceText(row({ confidence: 'low', sample_size: 12 }))).toBe(
      'низкое, выборка 12',
    )
  })

  it('различает «мало данных» и «данных не было вовсе»', () => {
    expect(
      confidenceText(row({ score: null, confidence: 'insufficient', sample_size: 4 })),
    ).toBe('недостаточно данных: 4 отправлений')
    expect(
      confidenceText(row({ score: null, confidence: 'insufficient', sample_size: 0 })),
    ).toBe('недостаточно данных: отправлений ещё не было')
  })
})

describe('byScore', () => {
  it('оценённые сверху по убыванию, неоценённые — вниз группой', () => {
    const rows = [
      row({ carrier_name: 'Без оценки Б', score: null, confidence: 'insufficient' }),
      row({ carrier_name: 'ПЭК', score: 71 }),
      row({ carrier_name: 'Без оценки А', score: null, confidence: 'insufficient' }),
      row({ carrier_name: 'СДЭК', score: 84 }),
    ]
    expect(byScore(rows).map((r) => r.carrier_name)).toEqual([
      'СДЭК',
      'ПЭК',
      'Без оценки А',
      'Без оценки Б',
    ])
  })

  it('при равном скоре порядок по имени, а не случайный', () => {
    const rows = [row({ carrier_name: 'Почта России' }), row({ carrier_name: 'Деловые Линии' })]
    expect(byScore(rows).map((r) => r.carrier_name)).toEqual(['Деловые Линии', 'Почта России'])
  })

  it('исходный массив не трогается', () => {
    const rows = [row({ carrier_name: 'ПЭК', score: 71 }), row({ carrier_name: 'СДЭК' })]
    byScore(rows)
    expect(rows.map((r) => r.carrier_name)).toEqual(['ПЭК', 'СДЭК'])
  })
})

describe('COMPONENTS', () => {
  it('доля инцидентов — единственная, где рост значения хуже', () => {
    // Перепутать направление значит показать «хорошо» там, где плохо.
    const worse = COMPONENTS.filter((c) => !c.higherIsBetter).map((c) => c.key)
    expect(worse).toEqual(['incident_rate'])
  })

  it('покрывает все составляющие ответа сервера', () => {
    // Забытая составляющая — это молча спрятанная часть расшифровки,
    // ради которой экран и существует (FR-7.5).
    const keys = Object.keys(row().components).sort()
    expect(COMPONENTS.map((c) => c.key).sort()).toEqual(keys)
  })
})
