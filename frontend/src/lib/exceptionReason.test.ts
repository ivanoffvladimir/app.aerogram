import { describe, expect, it } from 'vitest'
import { EXCEPTION_REASONS, describeReason } from './exceptionReason'

describe('причины разбора', () => {
  it('идут от дорогой к дешёвой, как и на сервере', () => {
    expect(EXCEPTION_REASONS.map((reason) => reason.key)).toEqual([
      'deadline_passed',
      'problem_status',
      'stalled',
    ])
  })

  it('сорванный срок выделен как критичный', () => {
    expect(describeReason('deadline_passed').tone).toBe('critical')
    expect(describeReason('deadline_passed').label).toBe('Срок сорван')
  })

  it('незнакомая причина показывается кодом, а не исчезает', () => {
    // Строка пропала бы из таблицы молча, и оператор не узнал бы,
    // что с отправлением вообще что-то не так.
    expect(describeReason('customs_hold').label).toBe('customs_hold')
  })
})
