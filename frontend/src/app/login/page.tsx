'use client'

import { zodResolver } from '@hookform/resolvers/zod'
import { useRouter } from 'next/navigation'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { ApiError, request, tokens, type AuthResponse } from '@/api/client'
import styles from './page.module.css'

const schema = z.object({
  email: z.string().min(1, 'Укажите адрес').email('Укажите корректный адрес'),
  password: z.string().min(1, 'Введите пароль'),
  // Поле видно всем, а не только после отказа: ответ на неверный пароль
  // и на отсутствующий код одинаков намеренно, и различить их здесь нечем.
  mfaCode: z
    .string()
    .trim()
    .regex(/^(\d{6})?$/, 'Код состоит из шести цифр'),
})

type FormValues = z.infer<typeof schema>

export default function LoginPage() {
  const router = useRouter()
  const [formError, setFormError] = useState<string | null>(null)
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { email: '', password: '', mfaCode: '' },
  })

  const onSubmit = handleSubmit(async (values) => {
    setFormError(null)
    try {
      const auth = await request<AuthResponse>('/auth/login', {
        method: 'POST',
        body: {
          email: values.email,
          password: values.password,
          // Пустое поле — это «фактор не подключён», а не «код пустой».
          ...(values.mfaCode ? { mfa_code: values.mfaCode } : {}),
        },
      })
      tokens.save(auth)
      router.replace('/rate-shopping')
    } catch (error) {
      // Причина отказа не уточняется намеренно: разный текст для неизвестной
      // почты и неверного пароля позволяет перебирать существующие адреса.
      setFormError(
        error instanceof ApiError && error.status === 401
          ? 'Неверная почта, пароль или код второго фактора'
          : 'Не удалось войти. Попробуйте ещё раз',
      )
    }
  })

  return (
    <div className={styles.page}>
      {/* method="post" не для отправки на сервер, а на случай, когда скрипты
          не загрузились: при штатной отправке браузера GET положил бы пароль
          в строку запроса, а оттуда — в логи прокси и историю браузера. */}
      <form className={styles.card} method="post" onSubmit={onSubmit} noValidate>
        <h1>Вход в Aerogram Logistics OS</h1>
        {formError && (
          <div className={styles.formError} role="alert">
            {formError}
          </div>
        )}
        <div className={styles.field}>
          <label htmlFor="email">Электронная почта</label>
          <input id="email" type="email" autoComplete="username" {...register('email')} />
          {errors.email && <div className={styles.error}>{errors.email.message}</div>}
        </div>
        <div className={styles.field}>
          <label htmlFor="password">Пароль</label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            {...register('password')}
          />
          {errors.password && <div className={styles.error}>{errors.password.message}</div>}
        </div>
        <div className={styles.field}>
          <label htmlFor="mfaCode">Код второго фактора</label>
          <input
            id="mfaCode"
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            placeholder="Если подключён"
            {...register('mfaCode')}
          />
          {errors.mfaCode && <div className={styles.error}>{errors.mfaCode.message}</div>}
        </div>
        <button className={styles.submit} type="submit" disabled={isSubmitting}>
          {isSubmitting ? 'Входим…' : 'Войти'}
        </button>
      </form>
    </div>
  )
}
