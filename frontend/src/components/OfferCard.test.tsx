import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { RateOffer } from '@/api/client'
import { OfferCard } from './OfferCard'

function offer(overrides: Partial<RateOffer> = {}): RateOffer {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    carrier_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    carrier_name: 'СДЭК',
    service_code: '136',
    service_name: 'Посылка дверь-дверь',
    source: 'client_contract',
    total_cost: { amount_minor: 245_050, currency: 'RUB' },
    eligible: true,
    ...overrides,
  }
}

describe('OfferCard', () => {
  it('показывает цену из минорных единиц', () => {
    render(<OfferCard offer={offer()} />)
    expect(screen.getByText(/2\s?450,50/)).toBeInTheDocument()
  })

  it('маркирует источник тарифа', () => {
    // Договор клиента и тариф платформы должны различаться визуально
    // (фронт-ТЗ, раздел 4).
    render(<OfferCard offer={offer()} />)
    expect(screen.getByText('Договор клиента')).toBeInTheDocument()
  })

  it('непригодный вариант не скрывается, а называет причину', () => {
    // Продуктовое ТЗ, раздел 7: опоздавшие показываются ниже, а не пропадают.
    render(
      <OfferCard
        offer={offer({
          eligible: false,
          ineligibility_reason: 'misses_deadline',
          lateness_seconds: 86_400,
        })}
      />,
    )
    expect(screen.getByText(/Не укладывается в срок/)).toBeInTheDocument()
    expect(screen.getByText(/1 сутки/)).toBeInTheDocument()
  })

  it('не даёт выбрать непригодный вариант', () => {
    const onSelect = vi.fn()
    render(
      <OfferCard
        offer={offer({ eligible: false, ineligibility_reason: 'misses_deadline' })}
        onSelect={onSelect}
      />,
    )
    expect(screen.getByRole('button', { name: /Выбрать/ })).toBeDisabled()
  })

  it('раскрывает расшифровку стоимости по требованию', async () => {
    const user = userEvent.setup()
    render(
      <OfferCard
        offer={offer({
          cost_components: [
            { type: 'base', money: { amount_minor: 214_000, currency: 'RUB' } },
            {
              type: 'insurance',
              money: { amount_minor: 31_050, currency: 'RUB' },
              rate_percent: 0.18,
            },
          ],
        })}
      />,
    )

    expect(screen.queryByText('Страхование')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Расшифровка стоимости' }))
    expect(screen.getByText('Базовый тариф')).toBeInTheDocument()
    expect(screen.getByText(/Страхование \(0.18%\)/)).toBeInTheDocument()
  })

  it('отсутствие данных показывает прочерком, а не нулём', () => {
    // Ноль процентов вероятности и «данных нет» — разные утверждения.
    render(<OfferCard offer={offer()} />)
    const values = screen.getAllByText('—')
    expect(values.length).toBeGreaterThan(0)
  })
})
