import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Counterparty } from '@/api/client'
import { AddressBookEdit } from './AddressBookEdit'

const COUNTERPARTY: Counterparty = {
  id: 'c1',
  type: 'legal',
  name: 'ООО «Роспломба»',
  inn: '7701234567',
  kpp: '770101001',
  contact_person: 'Иванов Иван',
  phone: '+79161234567',
  email: null,
  addresses: [
    {
      id: 'a1',
      counterparty_id: 'c1',
      label: null,
      country_code: 'RU',
      region: null,
      city: 'Москва',
      postal_code: null,
      street: 'ул Тверская',
      house: '1',
      flat: null,
      is_default_sender: true,
    },
  ],
}

function setup(overrides: Partial<Parameters<typeof AddressBookEdit>[0]> = {}) {
  const props = {
    counterparty: COUNTERPARTY,
    pending: false,
    error: null,
    onSaveContacts: vi.fn(),
    onSaveAddress: vi.fn(),
    onDone: vi.fn(),
    ...overrides,
  }
  render(<AddressBookEdit {...props} />)
  return props
}

describe('AddressBookEdit', () => {
  it('шлёт только изменённое поле контрагента', async () => {
    const user = userEvent.setup()
    const props = setup()

    await user.clear(screen.getByLabelText('Контактное лицо'))
    await user.type(screen.getByLabelText('Контактное лицо'), 'Петров Пётр')
    await user.click(screen.getByRole('button', { name: 'Сохранить' }))

    expect(props.onSaveContacts).toHaveBeenCalledWith({ contact_person: 'Петров Пётр' })
    expect(props.onSaveAddress).not.toHaveBeenCalled()
  })

  it('стёртый телефон уходит как очистка', async () => {
    const user = userEvent.setup()
    const props = setup()

    await user.clear(screen.getByLabelText('Телефон'))
    await user.click(screen.getByRole('button', { name: 'Сохранить' }))

    expect(props.onSaveContacts).toHaveBeenCalledWith({ phone: null })
  })

  it('правка адреса уходит своим запросом, с его идентификатором', async () => {
    const user = userEvent.setup()
    const props = setup()

    await user.type(screen.getByLabelText('Квартира'), '5')
    await user.click(screen.getByRole('button', { name: 'Сохранить' }))

    expect(props.onSaveAddress).toHaveBeenCalledWith('a1', { flat: '5' })
    expect(props.onSaveContacts).not.toHaveBeenCalled()
  })

  it('без единой правки запрос не уходит вовсе', async () => {
    // Пустой PATCH трогает updated_at и аудит, ничего не изменив.
    const user = userEvent.setup()
    const props = setup()

    await user.click(screen.getByRole('button', { name: 'Сохранить' }))

    expect(props.onSaveContacts).not.toHaveBeenCalled()
    expect(props.onSaveAddress).not.toHaveBeenCalled()
    expect(screen.getByText('Ничего не изменилось.')).toBeInTheDocument()
  })

  it('ИНН править нечем — поля нет на форме', () => {
    setup()
    expect(screen.queryByLabelText('ИНН')).not.toBeInTheDocument()
  })

  it('показывает ошибку сервера', () => {
    setup({ error: 'Поле «name» нельзя очистить' })
    expect(screen.getByRole('alert')).toHaveTextContent('нельзя очистить')
  })
})
