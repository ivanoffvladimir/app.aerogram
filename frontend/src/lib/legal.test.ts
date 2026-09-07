import { describe, expect, it } from 'vitest'
import {
  CONSENT,
  LEGAL_DOCUMENTS,
  OPERATOR_PLACEHOLDER,
  PRIVACY_POLICY,
  RETENTION_YEARS,
} from './legal'

/**
 * Тексты проверяются не на красоту, а на то, что ломается молча: пустой
 * раздел, потерянный получатель данных и черновик, выданный за действующую
 * редакцию.
 */
describe('юридические документы', () => {
  it('ни один раздел не пуст', () => {
    // Пустой раздел в политике читается как «здесь ничего не происходит»,
    // а это утверждение, которого делать нельзя.
    for (const document of LEGAL_DOCUMENTS) {
      for (const section of document.sections) {
        const filled = (section.body?.length ?? 0) + (section.items?.length ?? 0)
        expect(filled, `${document.slug}: «${section.heading}»`).toBeGreaterThan(0)
      }
    }
  })

  it('черновик помечен версией, по которой это видно', () => {
    // Компонент рисует предупреждение по префиксу версии: разойдись они,
    // черновик показался бы действующим документом.
    for (const document of LEGAL_DOCUMENTS) {
      expect(document.version).toMatch(/^draft/)
    }
  })

  it('реквизиты оператора не выдуманы, а оставлены человеку', () => {
    const text = JSON.stringify(LEGAL_DOCUMENTS)
    expect(text).toContain(OPERATOR_PLACEHOLDER)
  })
})

describe('политика обработки', () => {
  const text = JSON.stringify(PRIVACY_POLICY)

  it.each([
    ['СДЭК'],
    ['Деловые Линии'],
    ['ПЭК'],
    ['Почта России'],
    ['Яндекс Доставка'],
    ['Major Express'],
    ['ДаData'],
  ])('называет получателя данных: %s', (recipient) => {
    // Опись `docs/legal/personal-data-inventory.md` выведена из кода:
    // это те, кому данные действительно уходят. Политика, умалчивающая
    // о получателе, неверна — а проверить это глазами через год не выйдет.
    expect(text).toContain(recipient)
  })

  it('называет срок хранения перевозочных документов', () => {
    // «Храним, пока нужно» сроком не является: статья 5 закона требует
    // названного. Число берётся из константы, а не пишется в тексте
    // руками — иначе оно разойдётся с кодом и соврёт.
    expect(RETENTION_YEARS).toBeGreaterThanOrEqual(5)
    expect(text).toContain(`не менее ${RETENTION_YEARS} лет`)
  })

  it('обещает поиск и доступ в течение срока', () => {
    // Хранить без возможности найти — не исполнить обязанность.
    expect(text).toContain('поиска')
  })

  it('ссылается на закон, во исполнение которого опубликована', () => {
    expect(text).toContain('152-ФЗ')
    expect(text).toContain('18.1')
  })
})

describe('согласие', () => {
  it('называет срок и порядок отзыва', () => {
    // Согласие без порядка отзыва не является согласием по смыслу закона.
    const headings = CONSENT.sections.map((section) => section.heading)
    expect(headings.some((heading) => heading.includes('отзыв'))).toBe(true)
  })
})
