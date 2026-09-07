'use client'

import { useState } from 'react'
import type { MfaSetup } from '@/api/client'
import { ErrorNote } from './ErrorNote'
import { QrCode } from './QrCode'
import styles from './MfaSettings.module.css'
import buttons from '@/styles/buttons.module.css'

interface Props {
  enabled: boolean
  pending: boolean
  onSetup: () => Promise<MfaSetup>
  onEnable: (code: string) => Promise<void>
  onDisable: (code: string) => Promise<void>
}

/** Секрет группами по четыре: так его набирают руками без ошибок. */
export function groupSecret(secret: string): string {
  return (
    secret
      .replace(/\s+/g, '')
      .match(/.{1,4}/g)
      ?.join(' ') ?? secret
  )
}

/**
 * Подключение и отключение второго фактора.
 *
 * Три способа положить секрет в телефон, все рядом: QR-код для камеры
 * (ADR-0024), ключ для ручного ввода — его принимает любое
 * приложение-аутентификатор, — и ссылка `otpauth://`, которая на самом
 * телефоне открывает приложение напрямую. Ни один не вместо другого:
 * камера бывает не у всех, а ключ работает везде.
 */
export function MfaSettings({ enabled, pending, onSetup, onEnable, onDisable }: Props) {
  const [setup, setSetup] = useState<MfaSetup | null>(null)
  const [code, setCode] = useState('')
  // Хранится САМА ошибка, а не её текст: из текста уже не достать
  // идентификатор запроса. Сообщения проверки формы кладутся сюда
  // строкой и показываются как есть.
  const [error, setError] = useState<unknown>(null)

  async function run(action: () => Promise<unknown>) {
    setError(null)
    try {
      await action()
    } catch (caught) {
      setError(caught)
    }
  }

  function requireCode(): string | null {
    const trimmed = code.trim()
    if (!/^\d{6}$/.test(trimmed)) {
      setError('Введите шесть цифр из приложения-аутентификатора')
      return null
    }
    return trimmed
  }

  if (enabled) {
    return (
      <section className={styles.panel} aria-labelledby="mfa-title">
        <h2 id="mfa-title">Второй фактор</h2>
        <p className={styles.ok}>Подключён. Вход требует код из приложения-аутентификатора.</p>
        <p className={styles.hint}>
          Чтобы отключить, введите действующий код: иначе отключить фактор могла бы любая
          сессия, оставшаяся открытой.
        </p>
        <form
          className={styles.row}
          onSubmit={(event) => {
            event.preventDefault()
            const value = requireCode()
            if (value) void run(() => onDisable(value).then(() => setCode('')))
          }}
        >
          <label htmlFor="mfa-code">Код</label>
          <input
            id="mfa-code"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={code}
            onChange={(event) => setCode(event.target.value)}
          />
          <button type="submit" className={buttons.danger} disabled={pending}>
            Отключить
          </button>
        </form>
        {error ? <ErrorNote error={error} /> : null}
      </section>
    )
  }

  return (
    <section className={styles.panel} aria-labelledby="mfa-title">
      <h2 id="mfa-title">Второй фактор</h2>
      {!setup ? (
        <>
          <p className={styles.hint}>
            Не подключён. Код из приложения-аутентификатора будет запрашиваться при каждом
            входе.
          </p>
          <button
            type="button"
            className={buttons.primary}
            disabled={pending}
            onClick={() => void run(async () => setSetup(await onSetup()))}
          >
            Подключить
          </button>
        </>
      ) : (
        <>
          <p className={styles.warn}>
            Секрет показывается один раз. Сохраните его в приложении до того, как закроете
            страницу: повторно сервер его не покажет.
          </p>
          <div className={styles.qr}>
            <QrCode value={setup.otpauth_url} label="QR-код для приложения-аутентификатора" />
            <span className={styles.hint}>Наведите камеру приложения-аутентификатора.</span>
          </div>
          <dl className={styles.secret}>
            <dt>Ключ для ручного ввода</dt>
            <dd>
              <code>{groupSecret(setup.secret)}</code>
            </dd>
            <dt>Или откройте на телефоне</dt>
            <dd>
              <a href={setup.otpauth_url}>Добавить в приложение-аутентификатор</a>
            </dd>
          </dl>
          <form
            className={styles.row}
            onSubmit={(event) => {
              event.preventDefault()
              const value = requireCode()
              if (value) {
                void run(() =>
                  onEnable(value).then(() => {
                    setSetup(null)
                    setCode('')
                  }),
                )
              }
            }}
          >
            <label htmlFor="mfa-code">Код из приложения</label>
            <input
              id="mfa-code"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={code}
              onChange={(event) => setCode(event.target.value)}
            />
            <button type="submit" className={buttons.primary} disabled={pending}>
              Включить
            </button>
          </form>
        </>
      )}
      {error ? <ErrorNote error={error} /> : null}
    </section>
  )
}
