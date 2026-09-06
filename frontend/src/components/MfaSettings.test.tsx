import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { MfaSettings, groupSecret } from './MfaSettings'

const SETUP = {
  secret: 'JBSWY3DPEHPK3PXP',
  otpauth_url: 'otpauth://totp/Aerogram:a@example.com',
}

function renderOff(overrides: Partial<Parameters<typeof MfaSettings>[0]> = {}) {
  const props = {
    enabled: false,
    pending: false,
    onSetup: vi.fn().mockResolvedValue(SETUP),
    onEnable: vi.fn().mockResolvedValue(undefined),
    onDisable: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }
  render(<MfaSettings {...props} />)
  return props
}

describe('groupSecret', () => {
  it('режет ключ на четвёрки для ручного ввода', () => {
    expect(groupSecret('JBSWY3DPEHPK3PXP')).toBe('JBSW Y3DP EHPK 3PXP')
  })
})

describe('MfaSettings', () => {
  it('после подключения показывает секрет один раз и ссылку для телефона', async () => {
    const user = userEvent.setup()
    const props = renderOff()

    await user.click(screen.getByRole('button', { name: 'Подключить' }))

    expect(props.onSetup).toHaveBeenCalledOnce()
    expect(await screen.findByText('JBSW Y3DP EHPK 3PXP')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /аутентификатор/ })).toHaveAttribute(
      'href',
      SETUP.otpauth_url,
    )
    // Предупреждение стоит до поля кода: повторно сервер секрет не покажет.
    expect(screen.getByText(/показывается один раз/)).toBeInTheDocument()
  })

  it('не включает без шести цифр', async () => {
    const user = userEvent.setup()
    const props = renderOff()
    await user.click(screen.getByRole('button', { name: 'Подключить' }))
    await screen.findByText('JBSW Y3DP EHPK 3PXP')

    await user.type(screen.getByLabelText('Код из приложения'), '12')
    await user.click(screen.getByRole('button', { name: 'Включить' }))

    expect(props.onEnable).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('шесть цифр')
  })

  it('включает кодом и убирает секрет с экрана', async () => {
    const user = userEvent.setup()
    const props = renderOff()
    await user.click(screen.getByRole('button', { name: 'Подключить' }))
    await screen.findByText('JBSW Y3DP EHPK 3PXP')

    await user.type(screen.getByLabelText('Код из приложения'), '123456')
    await user.click(screen.getByRole('button', { name: 'Включить' }))

    expect(props.onEnable).toHaveBeenCalledWith('123456')
    // Секрет не должен оставаться на экране после включения.
    expect(screen.queryByText('JBSW Y3DP EHPK 3PXP')).not.toBeInTheDocument()
  })

  it('показывает ошибку сервера, а не глотает её', async () => {
    const user = userEvent.setup()
    // Настоящая ApiError, а не голый объект: раньше двойник проходил
    // только из-за приведения типа без проверки, и тест не касался того
    // пути, которым ошибка идёт в бою.
    renderOff({
      onSetup: vi.fn().mockRejectedValue(
        new ApiError(409, {
          error: {
            code: 'conflict',
            message: 'Второй фактор уже подключён',
            field: null,
            carrier_code: null,
            request_id: 'IIuhgQW2Qhn5AR7F',
          },
        }),
      ),
    })

    await user.click(screen.getByRole('button', { name: 'Подключить' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Второй фактор уже подключён')
  })

  it('к ошибке сервера прилагается идентификатор запроса', async () => {
    // Фронт-ТЗ, раздел 3: по нему поддержка находит запрос в логах.
    const user = userEvent.setup()
    renderOff({
      onSetup: vi.fn().mockRejectedValue(
        new ApiError(409, {
          error: {
            code: 'conflict',
            message: 'Второй фактор уже подключён',
            field: null,
            carrier_code: null,
            request_id: 'IIuhgQW2Qhn5AR7F',
          },
        }),
      ),
    })

    await user.click(screen.getByRole('button', { name: 'Подключить' }))
    await user.click(await screen.findByText('Технические подробности'))

    expect(screen.getByText('IIuhgQW2Qhn5AR7F')).toBeInTheDocument()
  })

  it('сообщение проверки формы показывается как есть', async () => {
    // Оно наше, а не серверное, и подменять его общим текстом значило бы
    // спрятать единственную подсказку, которая человеку и нужна.
    const user = userEvent.setup()
    renderOff({ enabled: true })

    await user.click(screen.getByRole('button', { name: 'Отключить' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('шесть цифр')
  })

  it('отключение требует действующий код', async () => {
    const user = userEvent.setup()
    const props = renderOff({ enabled: true })

    await user.click(screen.getByRole('button', { name: 'Отключить' }))
    expect(props.onDisable).not.toHaveBeenCalled()

    await user.type(screen.getByLabelText('Код'), '654321')
    await user.click(screen.getByRole('button', { name: 'Отключить' }))
    expect(props.onDisable).toHaveBeenCalledWith('654321')
  })
})
