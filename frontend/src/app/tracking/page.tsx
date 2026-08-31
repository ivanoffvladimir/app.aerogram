'use client'

import { useQuery } from '@tanstack/react-query'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useEffect } from 'react'
import { request, tokens, type ApiError, type ShipmentExceptionsPage } from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { EXCEPTION_REASONS, describeReason } from '@/lib/exceptionReason'
import { formatDateTime } from '@/lib/format'
import { SHIPMENT_STATUS_LABELS } from '@/lib/shipmentStatus'
import styles from './page.module.css'

// Модули CSS типизированы как `string | undefined`: класса может не быть.
const TONE_CLASS: Record<string, string | undefined> = {
  critical: styles.badgeCritical,
  warn: styles.badgeWarn,
  muted: styles.badgeMuted,
}

// Обновление на фоне: экран разбора держат открытым, и устаревший список
// на нём хуже пустого — по нему принимают решение звонить или не звонить.
const REFRESH_MS = 60_000

export default function TrackingPage() {
  const router = useRouter()

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const exceptions = useQuery({
    queryKey: ['tracking-exceptions'],
    queryFn: () => request<ShipmentExceptionsPage>('/tracking/exceptions'),
    refetchInterval: REFRESH_MS,
  })

  const items = exceptions.data?.items ?? []

  return (
    <AppShell>
      <h1>Разбор</h1>
      <p className={styles.counterLabel}>
        Едущие отправления, с которыми что-то не так. Доставленные и отменённые сюда не
        попадают.
      </p>

      <div className={styles.counters}>
        {EXCEPTION_REASONS.map((reason) => (
          <div
            key={reason.key}
            className={`${styles.counter} ${
              reason.key === 'deadline_passed' ? (styles.counterCritical ?? '') : ''
            }`}
          >
            <div className={styles.counterValue}>
              {exceptions.data?.by_reason[reason.key] ?? 0}
            </div>
            <div className={styles.counterLabel}>{reason.label}</div>
            <div className={styles.counterLabel}>{reason.hint}</div>
          </div>
        ))}
      </div>

      {exceptions.data?.truncated && (
        <div className={styles.notice} role="status">
          Просмотрены не все отправления: список ограничен {exceptions.data.scanned} строками.
          Разберите показанное — остальное появится следующим заходом.
        </div>
      )}

      {exceptions.isError && (
        <div role="alert" className={styles.empty}>
          {(exceptions.error as ApiError).message}
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
              <th>Последнее событие</th>
              <th>Что случилось</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td className={styles.number}>
                  <Link href={`/shipments/view?id=${item.id}`}>{item.number}</Link>
                </td>
                <td>{item.carrier_name ?? '—'}</td>
                <td>{SHIPMENT_STATUS_LABELS[item.status] ?? item.status}</td>
                <td>{item.tracking_number ?? '—'}</td>
                <td
                  className={item.reasons.includes('deadline_passed') ? styles.late : undefined}
                >
                  {formatDateTime(item.deadline)}
                </td>
                {/* Отсутствие событий — не «нет данных», а худший случай тишины. */}
                <td>
                  {item.last_event_at ? formatDateTime(item.last_event_at) : 'событий не было'}
                </td>
                <td>
                  <div className={styles.reasons}>
                    {item.reasons.map((reason) => {
                      const known = describeReason(reason)
                      return (
                        <span
                          key={reason}
                          className={`${styles.badge} ${TONE_CLASS[known.tone] ?? ''}`}
                          title={known.hint}
                        >
                          {known.label}
                        </span>
                      )
                    })}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {!exceptions.isLoading && items.length === 0 && (
          <div className={styles.empty}>
            {exceptions.isError ? 'Список не загружен' : 'Разбирать нечего: всё едет по плану'}
          </div>
        )}
      </div>
    </AppShell>
  )
}
