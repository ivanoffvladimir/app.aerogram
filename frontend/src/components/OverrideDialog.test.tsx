import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { RateOffer } from '@/api/client'
import { OverrideDialog } from './OverrideDialog'

const OFFER: RateOffer = {
  id: '22222222-2222-4222-8222-222222222222',
  carrier_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
  carrier_name: 'Деловые Линии',
  service_code: 'ltl',
  source: 'client_contract',
  total_cost: { amount_minor: 180_000, currency: 'RUB' },
  eligible: true,
}

beforeAll(() => {
  // jsdom не реализует showModal. Заглушка ОБЯЗАНА выставлять `open`:
  // без него содержимое диалога скрыто от дерева доступности, и тест
  // перестал бы отличать «диалог не открылся» от «кнопки нет».
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true
  }
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.open = false
  }
})

describe('OverrideDialog', () => {
  it('не пропускает подтверждение без причины', async () => {
    // Без причины Override Rate не раскладывается, а ради этого поле и есть.
    const user = userEvent.setup()
    const onConfirm = vi.fn()
    render(
      <OverrideDialog offer={OFFER} onCancel={vi.fn()} onConfirm={onConfirm} submitting={false} />,
    )

    await user.click(screen.getByRole('button', { name: 'Подтвердить выбор' }))

    expect(onConfirm).not.toHaveBeenCalled()
    expect(screen.getByText('Укажите причину')).toBeInTheDocument()
  })

  it('передаёт выбранную причину и комментарий', async () => {
    const user = userEvent.setup()
    const onConfirm = vi.fn()
    render(
      <OverrideDialog offer={OFFER} onCancel={vi.fn()} onConfirm={onConfirm} submitting={false} />,
    )

    await user.selectOptions(screen.getByLabelText('Причина'), 'recipient_requirement')
    await user.type(screen.getByLabelText(/Комментарий/), 'Получатель просил')
    await user.click(screen.getByRole('button', { name: 'Подтвердить выбор' }))

    expect(onConfirm).toHaveBeenCalledWith('recipient_requirement', 'Получатель просил')
  })

  it('показывает, от какого варианта отказывается оператор', () => {
    render(
      <OverrideDialog offer={OFFER} onCancel={vi.fn()} onConfirm={vi.fn()} submitting={false} />,
    )
    expect(screen.getByText(/Деловые Линии/)).toBeInTheDocument()
    expect(screen.getByText(/1\s?800,00/)).toBeInTheDocument()
  })
})
