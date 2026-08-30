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
})

type FormValues = z.infer<typeof schema>

export default function LoginPage() {
  const router = useRouter()
  const [formError, setFormError] = useState<string | null>(null)
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({ resolver: zodResolver(schema) })

  const onSubmit = handleSubmit(async (values) => {
    setFormError(null)
    try {
      const auth = await request<AuthResponse>('/auth/login', {
        method: 'POST',
        body: values,
      })
      tokens.save(auth)
      router.replace('/rate-shopping')
    } catch (error) {
      // Причина отказа не уточняется намеренно: разный текст для неизвестной
      // почты и неверного пароля позволяет перебирать существующие адреса.
      setFormError(
        error instanceof ApiError && error.status === 401
          ? 'Неверная почта или пароль'
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
        <button className={styles.submit} type="submit" disabled={isSubmitting}>
          {isSubmitting ? 'Входим…' : 'Войти'}
        </button>
      </form>
    </div>
  )
}
