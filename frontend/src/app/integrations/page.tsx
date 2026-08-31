'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import { request, tokens, type ApiError, type ApiKey, type ApiKeyCreated } from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { API_SCOPES, scopeLabel } from '@/lib/apiScope'
import { formatDateTime } from '@/lib/format'
import styles from './page.module.css'

export default function IntegrationsPage() {
  const router = useRouter()
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState<string[]>([])
  const [issued, setIssued] = useState<ApiKeyCreated | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const keys = useQuery({
    queryKey: ['api-keys'],
    queryFn: () => request<ApiKey[]>('/api-keys'),
  })

  const create = useMutation({
    mutationFn: () =>
      request<ApiKeyCreated>('/api-keys', { method: 'POST', body: { name, scopes } }),
    onSuccess: (created) => {
      // Значение показывается здесь и больше нигде: в базе только хеш.
      setIssued(created)
      setName('')
      setScopes([])
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    },
    onError: (cause) => setError((cause as ApiError).message),
  })

  const revoke = useMutation({
    mutationFn: (id: string) => request<void>(`/api-keys/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    },
    onError: (cause) => setError((cause as ApiError).message),
  })

  const toggle = (value: string) =>
    setScopes((current) =>
      current.includes(value) ? current.filter((s) => s !== value) : [...current, value],
    )

  return (
    <AppShell>
      <h1>Интеграции</h1>
      <p className={styles.intro}>
        Ключи для доступа к API из ERP, 1С и других систем. Ключ действует от имени компании и
        ограничен выбранными правами.
      </p>

      {issued && (
        <div className={styles.secret} role="status">
          <strong>Ключ «{issued.key.name}» выпущен. Сохраните его сейчас.</strong>
          <code className={styles.secretValue}>{issued.secret}</code>
          Показать значение повторно невозможно: в базе хранится только его отпечаток.
          Потерянный ключ отзывают и выпускают новый.
        </div>
      )}

      <form
        method="post"
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault()
          setError(null)
          create.mutate()
        }}
      >
        <div className={styles.field}>
          <label htmlFor="name">Название</label>
          <input
            id="name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Обмен с 1С"
            required
          />
        </div>

        <div className={styles.scopes}>
          {API_SCOPES.map((scope) => (
            <label key={scope.value} className={styles.scope}>
              <input
                type="checkbox"
                checked={scopes.includes(scope.value)}
                onChange={() => toggle(scope.value)}
              />
              <span>
                <span className={styles.scopeLabel}>{scope.label}</span>
                <br />
                <span className={styles.scopeHint}>{scope.hint}</span>
              </span>
            </label>
          ))}
        </div>

        {error && (
          <div className={styles.error} role="alert">
            {error}
          </div>
        )}

        {/* Ключ без прав ничего не может, поэтому кнопка недоступна:
            сервер такой запрос всё равно отклонит. */}
        <button type="submit" disabled={create.isPending || !name || scopes.length === 0}>
          {create.isPending ? 'Выпускаем…' : 'Выпустить ключ'}
        </button>
      </form>

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Название</th>
              <th>Начало ключа</th>
              <th>Права</th>
              <th>Последнее обращение</th>
              <th>Выпущен</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(keys.data ?? []).map((key) => (
              <tr key={key.id}>
                <td>{key.name}</td>
                <td className={styles.prefix}>{key.prefix}…</td>
                <td>
                  <div className={styles.badges}>
                    {key.scopes.map((scope) => (
                      <span key={scope} className={styles.badge}>
                        {scopeLabel(scope)}
                      </span>
                    ))}
                  </div>
                </td>
                {/* Ключом ни разу не пользовались — это тоже новость:
                    либо интеграция не настроена, либо ключ лишний. */}
                <td>
                  {key.last_used_at ? formatDateTime(key.last_used_at) : 'не использовался'}
                </td>
                <td>{formatDateTime(key.created_at)}</td>
                <td>
                  <button
                    type="button"
                    onClick={() => revoke.mutate(key.id)}
                    disabled={revoke.isPending}
                  >
                    Отозвать
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {!keys.isLoading && (keys.data?.length ?? 0) === 0 && (
          <div className={styles.empty}>Ключей нет. Внешние системы к API не подключены.</div>
        )}
      </div>
    </AppShell>
  )
}
