import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
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
    renderOff({
      onSetup: vi.fn().mockRejectedValue({ message: 'Второй фактор уже подключён' }),
    })

    await user.click(screen.getByRole('button', { name: 'Подключить' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Второй фактор уже подключён')
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
