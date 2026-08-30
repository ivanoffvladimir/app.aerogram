'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import { request, tokens, type ApiError, type User } from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { ROLE_LABELS, TENANT_ROLES } from '@/lib/directory'
import { formatDateTime } from '@/lib/format'
import styles from './page.module.css'

/** Минимальная длина пароля повторяет `UserCreate` бэкенда: форма обязана
 *  сказать об этом до отправки, а не показывать 422 после. */
const MIN_PASSWORD = 12

export default function UsersPage() {
  const router = useRouter()
  const client = useQueryClient()
  const [adding, setAdding] = useState(false)

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const me = useQuery({ queryKey: ['me'], queryFn: () => request<User>('/auth/me') })
  const users = useQuery({ queryKey: ['users'], queryFn: () => request<User[]>('/users') })

  const create = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      request<User>('/users', { method: 'POST', body }),
    onSuccess: async () => {
      setAdding(false)
      await client.invalidateQueries({ queryKey: ['users'] })
    },
  })

  // Право заводить пользователей проверяет сервер; кнопка лишь не предлагает
  // того, что заведомо получит 403. Пока роль неизвестна, кнопки нет —
  // показать её и убрать было бы хуже, чем показать позже.
  const isOwner = me.data?.role === 'owner'

  return (
    <AppShell>
      <div className={styles.head}>
        <h1>Пользователи</h1>
        {isOwner && (
          <button type="button" onClick={() => setAdding((open) => !open)}>
            {adding ? 'Отмена' : 'Добавить пользователя'}
          </button>
        )}
      </div>

      <p className={styles.note}>
        Роль определяет, что пользователь может сделать: логист считает и выбирает, оператор
        оформляет, наблюдатель только смотрит. Заводить и отключать людей вправе владелец.
      </p>

      {adding && (
        <UserForm
          pending={create.isPending}
          error={create.error as ApiError | null}
          onSubmit={(body) => create.mutate(body)}
        />
      )}

      {users.isError && (
        <div role="alert" className={styles.empty}>
          {(users.error as ApiError).message}
        </div>
      )}

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Имя</th>
              <th>Почта</th>
              <th>Роль</th>
              <th>Двухфакторная</th>
              <th>Последний вход</th>
            </tr>
          </thead>
          <tbody>
            {(users.data ?? []).map((user) => (
              <tr key={user.id}>
                <td className={styles.name}>{user.full_name}</td>
                <td>{user.email}</td>
                <td>{ROLE_LABELS[user.role] ?? user.role}</td>
                <td>
                  {user.mfa_enabled ? (
                    'включена'
                  ) : (
                    /* Не украшение: вход владельца без второго фактора —
                       это то, о чём бэкенд предупреждает в журнале. */
                    <span className={styles.warn}>выключена</span>
                  )}
                </td>
                <td>{formatDateTime(user.last_login_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>

        {!users.isLoading && (users.data?.length ?? 0) === 0 && (
          <div className={styles.empty}>Пользователей пока нет.</div>
        )}
      </div>
    </AppShell>
  )
}

function UserForm({
  pending,
  error,
  onSubmit,
}: {
  pending: boolean
  error: ApiError | null
  onSubmit: (body: Record<string, unknown>) => void
}) {
  const [email, setEmail] = useState('')
  const [fullName, setFullName] = useState('')
  const [role, setRole] = useState<string>('logistician')
  const [password, setPassword] = useState('')

  const tooShort = password.length > 0 && password.length < MIN_PASSWORD

  return (
    <form
      className={styles.form}
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit({ email: email.trim(), full_name: fullName.trim(), role, password })
      }}
    >
      <div className={styles.field}>
        <label htmlFor="full_name">Имя</label>
        <input
          id="full_name"
          required
          value={fullName}
          onChange={(event) => setFullName(event.target.value)}
        />
      </div>
      <div className={styles.field}>
        <label htmlFor="email">Почта</label>
        <input
          id="email"
          type="email"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </div>
      <div className={styles.field}>
        <label htmlFor="role">Роль</label>
        {/* Список ролей закрытый: платформенных значений в нём нет физически,
            как и в ``TenantRole`` на бэкенде. */}
        <select id="role" value={role} onChange={(event) => setRole(event.target.value)}>
          {TENANT_ROLES.map((value) => (
            <option key={value} value={value}>
              {ROLE_LABELS[value]}
            </option>
          ))}
        </select>
      </div>
      <div className={styles.field}>
        <label htmlFor="password">Пароль</label>
        <input
          id="password"
          type="password"
          required
          minLength={MIN_PASSWORD}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        <span className={tooShort ? styles.warn : styles.hint}>
          не короче {MIN_PASSWORD} символов
        </span>
      </div>
      <button
        type="submit"
        disabled={pending || fullName.trim() === '' || password.length < MIN_PASSWORD}
      >
        {pending ? 'Сохраняем…' : 'Добавить'}
      </button>
      {error && (
        <p role="alert" className={styles.error}>
          {error.message}
        </p>
      )}
    </form>
  )
}
