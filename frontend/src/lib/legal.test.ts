import { describe, expect, it } from 'vitest'
import { CONSENT, LEGAL_DOCUMENTS, OPERATOR_PLACEHOLDER, PRIVACY_POLICY } from './legal'

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
