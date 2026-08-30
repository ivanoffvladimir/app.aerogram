import { describe, expect, it } from 'vitest'
import {
  EVENT_STATUS_LABELS,
  FINAL_STATUSES,
  SHIPMENT_STATUS_LABELS,
  statusTone,
} from './shipmentStatus'

describe('статусы отправления', () => {
  it('переводит все значения словаря контракта', () => {
    // Полнота словаря сторожится типом на этапе сборки; здесь — что переводы
    // непустые и русские, а не оставленные англоязычными заглушками.
    for (const [status, label] of Object.entries(SHIPMENT_STATUS_LABELS)) {
      expect(label, status).toMatch(/[А-Яа-я]/)
    }
  })

  it('отделяет проблему от нормального хода', () => {
    // Проблему и задержку оператор обязан замечать не читая.
    expect(statusTone('Exception')).toBe('warn')
    expect(statusTone('Delayed')).toBe('warn')
    expect(statusTone('Delivered')).toBe('ok')
    expect(statusTone('InTransit')).toBe('plain')
  })

  it('приглушает то, что уже никуда не едет', () => {
    expect(statusTone('Cancelled')).toBe('muted')
    expect(statusTone('Draft')).toBe('muted')
  })

  it('считает завершёнными только доставленное и отменённое', () => {
    // Совпадает с проверкой бэкенда: из остальных состояний отмена возможна,
    // и прятать кнопку значило бы решать за перевозчика.
    expect([...FINAL_STATUSES].sort()).toEqual(['Cancelled', 'Delivered'])
    expect(FINAL_STATUSES.has('Exception')).toBe(false)
    expect(FINAL_STATUSES.has('InTransit')).toBe(false)
  })
})

describe('статусы ленты', () => {
  it('переводит нормализованную модель целиком', () => {
    // Четырнадцать состояний раздела 9 ТЗ плюс наш собственный DRAFT.
    expect(Object.keys(EVENT_STATUS_LABELS)).toHaveLength(15)
    for (const [status, label] of Object.entries(EVENT_STATUS_LABELS)) {
      expect(label, status).toMatch(/[А-Яа-я]/)
    }
  })

  it('различает возврат и неудачную попытку вручения', () => {
    // На границе API они схлопываются в Exception, но в ленте обязаны
    // читаться по-разному: это разные события с разными последствиями.
    expect(EVENT_STATUS_LABELS.RETURNED).not.toBe(
      EVENT_STATUS_LABELS.DELIVERY_ATTEMPT_FAILED,
    )
  })
})
