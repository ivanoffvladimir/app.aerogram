'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter } from 'next/navigation'
import { useEffect } from 'react'
import { request, tokens, type MfaSetup, type User } from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { MfaSettings } from '@/components/MfaSettings'
import { ROLE_LABELS } from '@/lib/directory'
import { formatDateTime } from '@/lib/format'
import styles from './page.module.css'

export default function ProfilePage() {
  const router = useRouter()
  const client = useQueryClient()

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const me = useQuery({ queryKey: ['me'], queryFn: () => request<User>('/auth/me') })

  // Признак «включён» приходит с сервера: после включения или отключения
  // перечитывается `me`, а не выставляется локально — экран показывает
  // то, что есть, а не то, что мы надеемся получить.
  const setup = useMutation({
    mutationFn: () => request<MfaSetup>('/auth/mfa/setup', { method: 'POST' }),
  })
  const enable = useMutation({
    mutationFn: (code: string) =>
      request<User>('/auth/mfa/enable', { method: 'POST', body: { code } }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['me'] }),
  })
  const disable = useMutation({
    mutationFn: (code: string) =>
      request<User>('/auth/mfa/disable', { method: 'POST', body: { code } }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['me'] }),
  })

  const user = me.data

  return (
    <AppShell>
      <h1>Профиль</h1>
      {user ? (
        <dl className={styles.facts}>
          <dt>Имя</dt>
          <dd>{user.full_name}</dd>
          <dt>Почта</dt>
          <dd>{user.email}</dd>
          <dt>Роль</dt>
          <dd>{ROLE_LABELS[user.role] ?? user.role}</dd>
          <dt>Последний вход</dt>
          <dd>{user.last_login_at ? formatDateTime(user.last_login_at) : '—'}</dd>
        </dl>
      ) : (
        <p className={styles.muted}>{me.isError ? 'Профиль не загружен' : 'Загружаем…'}</p>
      )}

      {user ? (
        <MfaSettings
          enabled={user.mfa_enabled}
          pending={setup.isPending || enable.isPending || disable.isPending}
          onSetup={() => setup.mutateAsync()}
          onEnable={async (code) => {
            await enable.mutateAsync(code)
          }}
          onDisable={async (code) => {
            await disable.mutateAsync(code)
          }}
        />
      ) : null}
    </AppShell>
  )
}
