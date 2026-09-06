import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import { ErrorNote } from './ErrorNote'

function apiError(requestId: string | null = 'IIuhgQW2Qhn5AR7F'): ApiError {
  return new ApiError(502, {
    error: {
      code: 'carrier_error',
      message: 'Перевозчик вернул ошибку',
      field: null,
      carrier_code: 'cdek',
      request_id: requestId,
    },
  })
}

describe('ErrorNote', () => {
  it('текст ошибки виден сразу, подробности спрятаны', () => {
    // Оператору нужен текст; код и идентификатор — только когда он пишет
    // в поддержку. Развёрнутые, они превратили бы ошибку в стену строк.
    render(<ErrorNote error={apiError()} />)

    expect(screen.getByRole('alert')).toHaveTextContent('Перевозчик вернул ошибку')
    expect(screen.getByText('Технические подробности')).toBeInTheDocument()
    expect(screen.getByRole('group')).not.toHaveAttribute('open')
  })

  it('идентификатор запроса показывается — ради него всё и делалось', async () => {
    render(<ErrorNote error={apiError()} />)

    await userEvent.click(screen.getByText('Технические подробности'))

    expect(screen.getByText('IIuhgQW2Qhn5AR7F')).toBeInTheDocument()
  })

  it('ошибка сети не притворяется ответом сервера', () => {
    // Ни английского «Failed to fetch», ни технических подробностей,
    // которых нет: запрос до сервера не дошёл.
    render(<ErrorNote error={new TypeError('Failed to fetch')} />)

    expect(screen.getByRole('alert')).toHaveTextContent('связаться с сервером')
    expect(screen.queryByText('Технические подробности')).not.toBeInTheDocument()
  })

  it('роль alert стоит всегда: ошибку читает и программа чтения с экрана', () => {
    render(<ErrorNote error={apiError(null)} />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })
})
