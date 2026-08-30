'use client'

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import { request, tokens, type ApiError, type ShipmentPage } from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { formatDateTime, formatMoney } from '@/lib/format'
import { SHIPMENT_STATUS_LABELS, statusTone } from '@/lib/shipmentStatus'
import styles from './page.module.css'

const PAGE_SIZE = 25

// Модули CSS типизированы как `string | undefined`: класса может не быть.
const TONE_CLASS: Record<string, string | undefined> = {
  ok: styles.badgeOk,
  warn: styles.badgeWarn,
  muted: styles.badgeMuted,
  plain: '',
}

export default function ShipmentsPage() {
  const router = useRouter()
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const shipments = useQuery({
    queryKey: ['shipments', status, query, page],
    queryFn: () => {
      const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) })
      if (status) params.set('status', status)
      if (query) params.set('q', query)
      return request<ShipmentPage>(`/shipments?${params}`)
    },
    // Страница не мигает пустотой на время загрузки следующей: список
    // «прыгает» ровно тогда, когда оператор пытается по нему читать.
    placeholderData: keepPreviousData,
  })

  const total = shipments.data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <AppShell>
      <h1>Отправления</h1>

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
          <label htmlFor="q">Номер или трек</label>
          <input
            id="q"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="AG-… или трек перевозчика"
          />
        </div>
        <div className={styles.field}>
          <label htmlFor="status">Статус</label>
          <select
            id="status"
            value={status}
            onChange={(event) => {
              setPage(1)
              setStatus(event.target.value)
            }}
          >
            <option value="">Любой</option>
            {Object.entries(SHIPMENT_STATUS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        <button type="submit">Найти</button>
      </form>

      {shipments.isError && (
        <div role="alert" className={styles.empty}>
          {(shipments.error as ApiError).message}
        </div>
      )}

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Номер</th>
              <th>Перевозчик</th>
              <th>Статус</th>
              <th>Трек-номер</th>
              <th>Срок</th>
              <th>Стоимость</th>
              <th>Создано</th>
            </tr>
          </thead>
          <tbody>
            {(shipments.data?.items ?? []).map((shipment) => {
              const late =
                shipment.deadline !== null &&
                shipment.deadline !== undefined &&
                shipment.eta !== null &&
                shipment.eta !== undefined &&
                new Date(shipment.eta) > new Date(shipment.deadline)
              return (
                <tr key={shipment.id}>
                  <td className={styles.number}>
                    <Link href={`/shipments/view?id=${shipment.id}`}>{shipment.number}</Link>
                  </td>
                  <td>{shipment.carrier_name ?? '—'}</td>
                  <td>
                    <span
                      className={`${styles.badge} ${TONE_CLASS[statusTone(shipment.status)] ?? ''}`}
                    >
                      {SHIPMENT_STATUS_LABELS[shipment.status] ?? shipment.status}
                    </span>
                  </td>
                  <td>{shipment.tracking_number ?? '—'}</td>
                  <td className={late ? styles.late : undefined}>
                    {formatDateTime(shipment.deadline)}
                  </td>
                  <td>
                    {shipment.quoted_total_cost
                      ? formatMoney(shipment.quoted_total_cost)
                      : '—'}
                  </td>
                  <td>{formatDateTime(shipment.created_at)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>

        {!shipments.isLoading && (shipments.data?.items.length ?? 0) === 0 && (
          <div className={styles.empty}>
            {query || status
              ? 'По этим условиям ничего не нашлось.'
              : 'Отправлений пока нет. Они появляются здесь после подтверждения выбора на расчёте.'}
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
