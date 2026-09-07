import { describe, expect, it } from 'vitest'
import type { AutoDecision, Decision } from '@/api/client'
import { autoRuleSummary, decisionMaker, overrideSummary } from './decision'

function decision(overrides: Partial<Decision> = {}): Decision {
  return {
    id: 'd',
    recommendation_id: 'r',
    quote_id: 'q',
    selected_offer_id: 'o',
    mode: 'manual',
    actor_id: 'u',
    override: false,
    override_reason: null,
    override_comment: null,
    selection_rule: null,
    auto_select_rule_id: null,
    auto_select_rule_name: null,
    selection_version: null,
    decided_at: '2026-09-07T12:00:00Z',
    ...overrides,
  }
}

const AUTO: AutoDecision = {
  decision_id: 'd',
  selected_offer_id: 'o',
  rule: 'cheapest',
  rule_id: 'rule-1',
  rule_name: 'берём дешёвое',
  override: true,
  selection_version: 'selection-1.0.0',
  decided_at: '2026-09-07T12:00:00Z',
}

describe('decisionMaker', () => {
  it('называет оператора', () => {
    expect(decisionMaker(decision())).toBe('Оператор')
  })

  it('называет правило по имени', () => {
    // Имя, а не «автоматически»: за именем человек идёт на экран правил.
    expect(
      decisionMaker(
        decision({ mode: 'auto', actor_id: null, auto_select_rule_name: 'берём дешёвое' }),
      ),
    ).toBe('Правило «берём дешёвое»')
  })

  it('отличает интеграцию от правила', () => {
    // Оба машинные, но это разные разговоры в споре с клиентом.
    expect(decisionMaker(decision({ mode: 'auto', actor_id: null }))).toBe('Интеграция по API')
  })
})

describe('autoRuleSummary', () => {
  it('называет и имя правила, и его признак', () => {
    expect(autoRuleSummary(AUTO)).toBe('Правило «берём дешёвое»: самый дешёвый')
  })

  it('неизвестный признак показывает как есть', () => {
    // Словарь бэкенда мог пополниться раньше нашего: прочерк был бы хуже.
    expect(autoRuleSummary({ ...AUTO, rule: 'greenest' as AutoDecision['rule'] })).toBe(
      'Правило «берём дешёвое»: greenest',
    )
  })
})

describe('overrideSummary', () => {
  it('проговаривает согласие с рекомендацией целиком', () => {
    // Пустая клетка читалась бы как «неизвестно», а здесь известно.
    expect(overrideSummary(decision())).toBe('нет, выбран рекомендованный вариант')
  })

  it('переводит причину человека', () => {
    expect(overrideSummary(decision({ override: true, override_reason: 'cheaper' }))).toBe(
      'Дешевле',
    )
  })

  it('переводит причину правила автовыбора', () => {
    // Значение заведено, чтобы отличать выбор правила от мотива человека;
    // показать его машинным кодом значило бы потерять эту разницу на экране.
    expect(
      overrideSummary(
        decision({
          mode: 'auto',
          actor_id: null,
          override: true,
          override_reason: 'auto_select_rule',
        }),
      ),
    ).toBe('Правило автовыбора')
  })
})
