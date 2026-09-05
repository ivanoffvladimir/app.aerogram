import { describe, expect, it } from 'vitest'
import type { CarrierAnalytics } from '@/api/client'
import {
  BASIS_LABELS,
  COMPONENTS,
  basisText,
  byScore,
  confidenceText,
  scoreText,
} from './carrierScore'

function row(overrides: Partial<CarrierAnalytics> = {}): CarrierAnalytics {
  return {
    carrier_id: 'id',
    carrier_code: 'cdek',
    carrier_name: 'СДЭК',
    score: 84,
    confidence: 'high',
    basis: 'own',
    platform_sample_size: null,
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
    formula_version: 'score-2.0.0',
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
      'низкое, ваша выборка 12',
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

describe('основание оценки', () => {
  it('оценку платформы не выдаёт за собственную статистику клиента', () => {
    // Без этой подписи «87» у нового клиента читается как вывод из его
    // собственного опыта, которого ещё не было.
    const fresh = row({ basis: 'platform', sample_size: 0, platform_sample_size: 900 })
    expect(BASIS_LABELS[fresh.basis]).toBe('по платформе')
    expect(basisText(fresh)).toBe('900 отправлений платформы, ваших пока нет')
  })

  it('смешанное основание называет обе выборки', () => {
    const mixed = row({ basis: 'mixed', sample_size: 40, platform_sample_size: 900 })
    expect(basisText(mixed)).toBe('40 ваших и 900 по платформе')
  })

  it('без платформенной базы говорит только о своих', () => {
    expect(basisText(row({ basis: 'own', sample_size: 120 }))).toBe('120 ваших отправлений')
  })

  it('отсутствие данных где бы то ни было названо прямо', () => {
    const none = row({ basis: 'none', score: null, confidence: 'insufficient', sample_size: 0 })
    expect(basisText(none)).toBe('данных нет ни у вас, ни на платформе')
  })

  it('у каждого основания есть русская подпись', () => {
    for (const basis of ['platform', 'mixed', 'own', 'none'] as const) {
      expect(BASIS_LABELS[basis]).toBeTruthy()
    }
  })

  it('число клиентов платформы наружу не попадает', () => {
    // Сколько компаний возит этим перевозчиком — сведение о клиентской базе
    // платформы, а не о перевозчике.
    const text = basisText(
      row({ basis: 'platform', sample_size: 0, platform_sample_size: 900 }),
    )
    expect(text).not.toMatch(/клиент|компан/i)
  })
})

describe('доверие', () => {
  it('считается по своей выборке, а не по платформенной', () => {
    // Платформенная база делает оценку возможной, но не делает её более вашей.
    const fresh = row({ basis: 'platform', confidence: 'low', sample_size: 0 })
    expect(confidenceText(fresh)).toBe('низкое, ваша выборка 0')
  })
})
