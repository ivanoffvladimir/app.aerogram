'use client'

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import {
  request,
  tokens,
  type ApiError,
  type Counterparty,
  type CorePage,
} from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { COUNTERPARTY_TYPE_LABELS, formatAddress } from '@/lib/directory'
import styles from './page.module.css'

const PAGE_SIZE = 25

export default function CounterpartiesPage() {
  const router = useRouter()
  const client = useQueryClient()
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [opened, setOpened] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const counterparties = useQuery({
    queryKey: ['counterparties', query, page],
    queryFn: () => {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String((page - 1) * PAGE_SIZE),
      })
      if (query) params.set('q', query)
      return request<CorePage<Counterparty>>(`/counterparties?${params}`)
    },
    placeholderData: keepPreviousData,
  })

  const create = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      request<Counterparty>('/counterparties', { method: 'POST', body }),
    onSuccess: async () => {
      setAdding(false)
      await client.invalidateQueries({ queryKey: ['counterparties'] })
    },
  })

  const total = counterparties.data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <AppShell>
      <div className={styles.head}>
        <h1>Адресная книга</h1>
        <button type="button" onClick={() => setAdding((open) => !open)}>
          {adding ? 'Отмена' : 'Новый контрагент'}
        </button>
      </div>

      {adding && (
        <CounterpartyForm
          pending={create.isPending}
          error={create.error as ApiError | null}
          onSubmit={(body) => create.mutate(body)}
        />
      )}

      <form
        method="post"
        className={styles.filters}
        onSubmit={(event) => {
          event.preventDefault()
          setPage(1)
          setQuery(search)
        }}
      >
        <div className={styles.field}>
          <label htmlFor="q">Название или ИНН</label>
          <input
            id="q"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Роспломба или 7701234567"
          />
        </div>
        <button type="submit">Найти</button>
      </form>

      {counterparties.isError && (
        <div role="alert" className={styles.empty}>
          {(counterparties.error as ApiError).message}
        </div>
      )}

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Название</th>
              <th>Тип</th>
              <th>ИНН</th>
              <th>Контакт</th>
              <th>Адреса</th>
            </tr>
          </thead>
          <tbody>
            {(counterparties.data?.items ?? []).map((counterparty) => {
              const open = opened === counterparty.id
              return (
                <tr key={counterparty.id}>
                  <td>
                    <button
                      type="button"
                      className={styles.link}
                      aria-expanded={open}
                      onClick={() => setOpened(open ? null : counterparty.id)}
                    >
                      {counterparty.name}
                    </button>
                    {open && (
                      <ul className={styles.addresses}>
                        {counterparty.addresses.length === 0 && (
                          <li className={styles.muted}>Адресов пока нет.</li>
                        )}
                        {counterparty.addresses.map((address) => (
                          <li key={address.id}>
                            {address.label && <b>{address.label}: </b>}
                            {formatAddress(address)}
                            {address.is_default_sender && (
                              <span className={styles.badge}>адрес отправителя</span>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                  <td>
                    {COUNTERPARTY_TYPE_LABELS[counterparty.type] ?? counterparty.type}
                  </td>
                  <td>{counterparty.inn ?? '—'}</td>
                  <td>
                    {counterparty.contact_person ?? '—'}
                    {counterparty.phone && (
                      <span className={styles.muted}> · {counterparty.phone}</span>
                    )}
                  </td>
                  <td>{counterparty.addresses.length}</td>
                </tr>
              )
            })}
          </tbody>
        </table>

        {!counterparties.isLoading && (counterparties.data?.items.length ?? 0) === 0 && (
          <div className={styles.empty}>
            {query
              ? 'По этому запросу никого не нашлось.'
              : 'Контрагентов пока нет. Заведите первого — он появится в расчёте как отправитель или получатель.'}
          </div>
        )}
      </div>

      <div className={styles.pager}>
        <button type="button" disabled={page <= 1} onClick={() => setPage((n) => n - 1)}>
          Назад
        </button>
        <span>
          Страница {page} из {pages}, всего {total}
        </span>
        <button type="button" disabled={page >= pages} onClick={() => setPage((n) => n + 1)}>
          Вперёд
        </button>
      </div>
    </AppShell>
  )
}

function CounterpartyForm({
  pending,
  error,
  onSubmit,
}: {
  pending: boolean
  error: ApiError | null
  onSubmit: (body: Record<string, unknown>) => void
}) {
  const [type, setType] = useState('legal')
  const [name, setName] = useState('')
  const [inn, setInn] = useState('')
  const [contact, setContact] = useState('')
  const [phone, setPhone] = useState('')

  return (
    <form
      className={styles.form}
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit({
          type,
          name: name.trim(),
          // Пустая строка и «не указано» — разные вещи: ИНН, пришедший пустой
          // строкой, прошёл бы проверку формата и лёг бы в базу как пустой.
          inn: inn.trim() || null,
          contact_person: contact.trim() || null,
          phone: phone.trim() || null,
          addresses: [],
        })
      }}
    >
      <div className={styles.field}>
        <label htmlFor="type">Тип</label>
        <select id="type" value={type} onChange={(event) => setType(event.target.value)}>
          {Object.entries(COUNTERPARTY_TYPE_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </div>
      <div className={styles.field}>
        <label htmlFor="name">Название</label>
        <input
          id="name"
          required
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </div>
      <div className={styles.field}>
        <label htmlFor="inn">ИНН</label>
        <input id="inn" value={inn} onChange={(event) => setInn(event.target.value)} />
      </div>
      <div className={styles.field}>
        <label htmlFor="contact">Контактное лицо</label>
        <input
          id="contact"
          value={contact}
          onChange={(event) => setContact(event.target.value)}
        />
      </div>
      <div className={styles.field}>
        <label htmlFor="phone">Телефон</label>
        <input id="phone" value={phone} onChange={(event) => setPhone(event.target.value)} />
      </div>
      <button type="submit" disabled={pending || name.trim() === ''}>
        {pending ? 'Сохраняем…' : 'Сохранить'}
      </button>
      {error && (
        <p role="alert" className={styles.error}>
          {error.message}
        </p>
      )}
    </form>
  )
}
