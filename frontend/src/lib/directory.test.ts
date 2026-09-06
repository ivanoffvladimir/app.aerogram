import { describe, expect, it } from 'vitest'
import {
  ACCOUNT_STATUS_LABELS,
  CARRIER_MODE_LABELS,
  COUNTERPARTY_TYPE_LABELS,
  formatAddress,
  healthText,
  ROLE_LABELS,
  TENANT_ROLES,
} from './directory'

const ADDRESS = {
  region: 'Приморский край',
  city: 'Владивосток',
  street: 'ул. Примерная',
  house: '1',
  flat: null,
  postal_code: '690000',
}

describe('справочники', () => {
  it('покрывает все типы контрагента из проверки бэкенда', () => {
    // Значения продублированы намеренно: тест обязан упасть, если словарь
    // разойдётся с counterparty_type в core/models.py.
    expect(Object.keys(COUNTERPARTY_TYPE_LABELS).sort()).toEqual([
      'entrepreneur',
      'individual',
      'legal',
    ])
  })

  it('предлагает только роли, которые владелец вправе выдать', () => {
    expect([...TENANT_ROLES]).toEqual(['owner', 'logistician', 'operator'])
    expect(TENANT_ROLES).not.toContain('platform_admin')
    expect(TENANT_ROLES).not.toContain('support')
  })

  it('называет по-русски и те роли, которые выдать нельзя', () => {
    // Существующего пользователя с платформенной ролью показать надо,
    // иначе в таблице окажется английское слово.
    for (const role of ['owner', 'logistician', 'operator', 'viewer', 'api_client']) {
      expect(ROLE_LABELS[role]).toBeTruthy()
    }
  })
})

describe('адрес одной строкой', () => {
  it('собирает то, что есть, и не выдумывает разделителей', () => {
    expect(formatAddress(ADDRESS)).toBe(
      '690000, Приморский край, Владивосток, ул. Примерная, 1',
    )
  })

  it('пропускает пустые части, а не оставляет запятые', () => {
    expect(formatAddress({ ...ADDRESS, region: null, postal_code: null, house: null })).toBe(
      'Владивосток, ул. Примерная',
    )
  })

  it('называет квартиру, а не приписывает её к дому', () => {
    // «1, 5» читалось бы как два дома.
    expect(formatAddress({ ...ADDRESS, flat: '5' })).toContain('1, кв. 5')
  })
})

describe('подключение перевозчика', () => {
  it('называет оба режима договора', () => {
    // Значения совпадают с проверкой carrier_account_mode в core/models.py.
    expect(Object.keys(CARRIER_MODE_LABELS).sort()).toEqual(['aerogram', 'own_contract'])
  })

  it('называет все состояния проверки доступов', () => {
    for (const status of ['unchecked', 'ok', 'error']) {
      expect(ACCOUNT_STATUS_LABELS[status]).toBeTruthy()
    }
  })
})

describe('healthText', () => {
  it('успешная проверка называет задержку', () => {
    // «В порядке» без числа не отличает перевозчика, отвечающего за 200 мс,
    // от отвечающего за 8 секунд, — а второй сорвёт общий дедлайн выдачи,
    // оставаясь формально исправным.
    expect(healthText({ is_healthy: true, latency_ms: 213, message: null })).toBe(
      'в порядке, ответ за 213 мс',
    )
  })

  it('отказ показывает причину, а не общее слово', () => {
    expect(
      healthText({
        is_healthy: false,
        latency_ms: 90,
        message: 'Перевозчик отклонил учётные данные',
      }),
    ).toBe('Перевозчик отклонил учётные данные')
  })

  it('отказ без причины всё равно называет себя отказом', () => {
    // Пустая ячейка читалась бы как «проверка не запускалась».
    expect(healthText({ is_healthy: false, latency_ms: 0, message: null })).toBe(
      'подключение не работает',
    )
  })
})
