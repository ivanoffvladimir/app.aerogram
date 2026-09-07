import { describe, expect, it } from 'vitest'
import type { ShipmentDocument } from '@/api/client'
import { documentState, documentTitle, isDownloadable } from './documentStatus'

function document(overrides: Partial<ShipmentDocument> = {}): ShipmentDocument {
  return {
    id: 'd',
    shipment_id: 's',
    type: 'label',
    format: 'pdf',
    status: 'ready',
    size_bytes: 1024,
    error: null,
    generated_at: '2026-09-07T12:00:00Z',
    created_at: '2026-09-07T12:00:00Z',
    ...overrides,
  }
}

describe('documentTitle', () => {
  it('называет тип и формат', () => {
    expect(documentTitle(document())).toBe('Этикетка · PDF')
  })

  it('неизвестный тип показывает как есть', () => {
    // Словарь бэкенда мог пополниться раньше нашего.
    expect(documentTitle(document({ type: 'waybill' }))).toBe('Накладная · PDF')
  })
})

describe('documentState', () => {
  it('различает «готовится» и «не будет готова»', () => {
    // По первому кладовщик ждёт, по второму идёт разбираться: свести их
    // к одному «нет файла» значило бы заставить ждать напрасно.
    expect(documentState(document({ status: 'pending' }))).toBe('перевозчик формирует форму')
    expect(documentState(document({ status: 'failed', error: 'Форма недоступна' }))).toBe(
      'Форма недоступна',
    )
  })

  it('показывает причину перевозчика, а не своё «не получилось»', () => {
    expect(documentState(document({ status: 'failed', error: 'Заказ ещё не принят' }))).toBe(
      'Заказ ещё не принят',
    )
  })

  it('отличает «отслужила» от «не получилось»', () => {
    // По «не получилось» кладовщик пойдёт разбираться с перевозчиком,
    // хотя груз давно доставлен, а файл просто удалён вместе
    // с персональными данными получателя.
    expect(documentState(document({ status: 'expired' }))).toBe('файл удалён: форма отслужила')
  })

  it('неудача без причины всё же что-то говорит', () => {
    // Ограничение схемы причину требует, но пустая строка в базе возможна,
    // а пустая клетка на экране читается как «неизвестно что произошло».
    expect(documentState(document({ status: 'failed', error: null }))).toBe(
      'не удалось получить форму',
    )
  })
})

describe('isDownloadable', () => {
  it('скачать можно только готовое', () => {
    expect(isDownloadable(document())).toBe(true)
    expect(isDownloadable(document({ status: 'pending' }))).toBe(false)
    expect(isDownloadable(document({ status: 'failed' }))).toBe(false)
    // Кнопка на удалённый файл вела бы в 409 — и выглядела бы поломкой.
    expect(isDownloadable(document({ status: 'expired' }))).toBe(false)
  })
})
