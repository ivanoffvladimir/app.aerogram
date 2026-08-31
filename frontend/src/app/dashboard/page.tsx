'use client'

import { useQuery } from '@tanstack/react-query'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import { request, tokens, type ApiError, type Summary } from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { describeReason } from '@/lib/exceptionReason'
import { formatMoney, formatRate } from '@/lib/format'
import { OVERRIDE_REASON_LABELS } from '@/lib/overrideReason'
import styles from './page.module.css'

const WINDOWS = [7, 30, 90, 365]

export default function DashboardPage() {
  const router = useRouter()
  const [days, setDays] = useState(30)

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const summary = useQuery({
    queryKey: ['summary', days],
    queryFn: () => request<Summary>(`/reports/summary?days=${days}`),
  })

  const data = summary.data
  const delivery = data?.delivery
  const overrides = data?.overrides

  return (
    <AppShell>
      <h1>Сводка</h1>

      <div className={styles.window}>
        <div className={styles.field}>
          <label htmlFor="days">Период</label>
          <select
            id="days"
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
          >
            {WINDOWS.map((value) => (
              <option key={value} value={value}>
                {value} дней
              </option>
            ))}
          </select>
        </div>
      </div>

      {summary.isError && (
        <div role="alert" className={styles.empty}>
          {(summary.error as ApiError).message}
        </div>
      )}

      <div className={styles.cards}>
        <div className={styles.card}>
          <div className={styles.value}>{formatRate(delivery?.on_time_rate)}</div>
          <div className={styles.label}>Доставлено в срок</div>
          {/* Знаменатель показан рядом: доля по трём доставкам и доля
              по трёмстам — разные основания для разговора с перевозчиком. */}
          <div className={styles.label}>
            {delivery ? `из ${delivery.with_deadline} со сроком` : ''}
          </div>
        </div>

        <div className={styles.card}>
          <div className={styles.value}>{delivery?.delivered ?? '—'}</div>
          <div className={styles.label}>Доставок за период</div>
          <div className={styles.label}>
            {delivery && delivery.late > 0 ? `опоздали ${delivery.late}` : 'без опозданий'}
          </div>
        </div>

        <div className={styles.card}>
          <div className={`${styles.value} ${data?.exceptions_total ? styles.valueAlarm : ''}`}>
            {data?.exceptions_total ?? '—'}
          </div>
          <div className={styles.label}>Требуют разбора сейчас</div>
          <div className={styles.label}>
            <Link href="/tracking">Открыть разбор</Link>
          </div>
        </div>

        <div className={styles.card}>
          <div className={styles.value}>{formatRate(overrides?.override_rate)}</div>
          <div className={styles.label}>Отказов от рекомендации</div>
          <div className={styles.label}>
            {overrides ? `из ${overrides.decisions} решений` : ''}
          </div>
        </div>
      </div>

      <section className={styles.section}>
        <h2>Расходы</h2>
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Валюта</th>
                <th className={styles.numeric}>Отправлений</th>
                <th className={styles.numeric}>По расчёту</th>
                <th className={styles.numeric}>По счетам</th>
                <th className={styles.numeric}>Счетов получено</th>
              </tr>
            </thead>
            <tbody>
              {(data?.costs ?? []).map((row) => (
                <tr key={row.currency}>
                  <td>{row.currency}</td>
                  <td className={styles.numeric}>{row.shipments}</td>
                  <td className={styles.numeric}>{formatMoney(row.quoted)}</td>
                  <td className={styles.numeric}>{formatMoney(row.actual)}</td>
                  <td className={styles.numeric}>
                    {row.with_actual} из {row.shipments}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!summary.isLoading && (data?.costs.length ?? 0) === 0 && (
            <div className={styles.empty}>За период отправлений не было</div>
          )}
        </div>
        <p className={styles.note}>
          Суммы по счетам неполны, пока счета не пришли: сравнивать их с расчётом можно только
          по тем отправлениям, где счёт уже есть.
        </p>
      </section>

      <section className={styles.section}>
        <h2>Отказы от рекомендации</h2>
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Причина</th>
                <th className={styles.numeric}>Решений</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(overrides?.by_reason ?? {}).map(([reason, count]) => (
                <tr key={reason}>
                  <td>{OVERRIDE_REASON_LABELS[reason] ?? reason}</td>
                  <td className={styles.numeric}>{count}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!summary.isLoading && Object.keys(overrides?.by_reason ?? {}).length === 0 && (
            <div className={styles.empty}>Рекомендацию принимали без исключений</div>
          )}
        </div>
      </section>

      <section className={styles.section}>
        <h2>Открытые исключения</h2>
        <div className={styles.cards}>
          {Object.entries(data?.exceptions ?? {}).map(([reason, count]) => {
            const known = describeReason(reason)
            return (
              <div key={reason} className={styles.card}>
                <div className={`${styles.value} ${count ? styles.valueAlarm : ''}`}>
                  {count}
                </div>
                <div className={styles.label}>{known.label}</div>
                <div className={styles.label}>{known.hint}</div>
              </div>
            )
          })}
        </div>
      </section>
    </AppShell>
  )
}
