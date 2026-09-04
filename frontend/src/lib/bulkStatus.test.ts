import { describe, expect, it } from 'vitest'
import { ROW_STATUS_LABELS, RUN_STATUS_LABELS, failedCount, nextStep } from './bulkStatus'

describe('nextStep', () => {
  it('ведёт прогон по шагам: посчитать, выбрать, оформить', () => {
    expect(nextStep({ new: 3 })?.path).toBe('quote')
    expect(nextStep({ quoted: 3 })?.path).toBe('select')
    expect(nextStep({ selected: 3 })?.path).toBe('create')
  })

  it('различает «посчитан» и «выбран» по строкам, а не по прогону', () => {
    // Сервер оставляет прогон в состоянии `quoted` и после расчёта,
    // и после выбора тарифа. Кнопка обязана различать эти два случая,
    // иначе оператору предлагается посчитать уже посчитанное.
    expect(nextStep({ quoted: 5 })?.label).toBe('Выбрать тариф')
    expect(nextStep({ selected: 5 })?.label).toBe('Оформить')
  })

  it('считает раньше, чем оформляет, если прогон частичный', () => {
    // Непосчитанная строка нужнее выбранной: пока она висит,
    // прогон не полон, и оформлять его рано.
    expect(nextStep({ new: 1, selected: 4 })?.path).toBe('quote')
    expect(nextStep({ quoted: 1, selected: 4 })?.path).toBe('select')
  })

  it('молчит, когда делать нечего', () => {
    expect(nextStep({ created: 10 })).toBeNull()
    expect(nextStep({ created: 8, failed: 2 })).toBeNull()
    expect(nextStep({ failed: 10 })).toBeNull()
    expect(nextStep({})).toBeNull()
  })

  it('не предлагает шаг из-за нулевого счётчика', () => {
    // Сервер присылает состояния с нулями; ноль это «таких строк нет».
    expect(nextStep({ new: 0, quoted: 0, selected: 0, created: 5 })).toBeNull()
  })
})

describe('failedCount', () => {
  it('ноль, когда о непрошедших строках сервер не написал', () => {
    expect(failedCount({ created: 4 })).toBe(0)
  })

  it('отдаёт число непрошедших строк', () => {
    expect(failedCount({ created: 4, failed: 2 })).toBe(2)
  })
})

describe('подписи', () => {
  it('покрывают все состояния прогона и строки', () => {
    // Пустая подпись означала бы пустую ячейку на экране: состояние есть,
    // а человеку не сказано какое.
    expect(Object.values(RUN_STATUS_LABELS).every(Boolean)).toBe(true)
    expect(Object.values(ROW_STATUS_LABELS).every(Boolean)).toBe(true)
  })
})
