'use client'

import { useQuery } from '@tanstack/react-query'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import {
  request,
  tokens,
  type ApiError,
  type Reconciliation,
  type ReconciliationState,
} from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { formatDateTime, formatMoney, formatRate } from '@/lib/format'
import {
  STATE_LABELS,
  STATE_ORDER,
  differenceTone,
  hasAnythingToReconcile,
  reconciledCount,
} from '@/lib/reconciliation'
import { SHIPMENT_STATUS_LABELS } from '@/lib/shipmentStatus'
import styles from './page.module.css'

/** Модули CSS типизированы как `string | undefined`: класса может не быть. */
const STATE_CLASS: Record<ReconciliationState, string | undefined> = {
  awaiting: styles.badgeWaiting,
  no_quote: styles.badgeWaiting,
  matched: styles.badgeMatched,
  overcharged: styles.badgeOver,
  undercharged: styles.badgeUnder,
}

const TONE_CLASS = {
  over: styles.over,
  under: styles.under,
  zero: undefined,
} as const

const PERIODS = [
  { days: 30, label: '30 дней' },
  { days: 90, label: '90 дней' },
  { days: 365, label: 'Год' },
]

export default function InvoicesPage() {
  const router = useRouter()

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const [days, setDays] = useState(30)
  const [state, setState] = useState<ReconciliationState | ''>('')

  const reconciliation = useQuery({
    queryKey: ['reconciliation', days, state],
    queryFn: () => {
      const params = new URLSearchParams({ days: String(days) })
      if (state) params.set('state', state)
      return request<Reconciliation>(`/billing/reconciliation?${params}`)
    },
  })

  const data = reconciliation.data
  const currencies = data?.currencies ?? []
  const items = data?.items ?? []
  const nothingReconciled = !!data && !hasAnythingToReconcile(currencies)

  return (
    <AppShell>
      <h1>Расходы и счета</h1>
      <p className={styles.hint}>
        Что обещал расчёт и что выставил перевозчик. Разница считается{' '}
        <b>по одним и тем же отправлениям</b>: те, по которым счёт ещё не пришёл, в неё не
        входят — иначе неоплаченное выглядело бы экономией.
      </p>

      <div className={styles.filters}>
        <div className={styles.tabs}>
          {PERIODS.map((period) => (
            <button
              key={period.days}
              type="button"
              className={`${styles.tab} ${days === period.days ? styles.tabActive : ''}`}
              onClick={() => setDays(period.days)}
            >
              {period.label}
            </button>
          ))}
        </div>
        <label className={styles.field}>
          <span className={styles.muted}>Состояние</span>
          <select
            value={state}
            onChange={(event) => setState(event.target.value as ReconciliationState | '')}
          >
            <option value="">Все</option>
            {STATE_ORDER.map((value) => (
              <option key={value} value={value}>
                {STATE_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
      </div>

      {reconciliation.isError && (
        <div role="alert" className={styles.empty}>
          {(reconciliation.error as ApiError).message}
        </div>
      )}

      {currencies.map((totals) => {
        const reconciled = reconciledCount(totals)
        return (
          <section key={totals.currency} className={styles.cards}>
            <div className={styles.card}>
              <span className={styles.cardLabel}>Расчёт за период</span>
              <span className={styles.cardValue}>{formatMoney(totals.quoted)}</span>
              <span className={styles.muted}>отправлений: {totals.shipments}</span>
            </div>
            <div className={styles.card}>
              <span className={styles.cardLabel}>Счета получены</span>
              <span className={styles.cardValue}>{formatMoney(totals.actual)}</span>
              <span className={styles.muted}>
                по {reconciled} из {totals.shipments}
              </span>
            </div>
            <div className={styles.card}>
              <span className={styles.cardLabel}>Расхождение</span>
              <span
                className={`${styles.cardValue} ${TONE_CLASS[differenceTone(totals.difference)] ?? ''}`}
              >
                {reconciled ? formatMoney(totals.difference) : '—'}
              </span>
              <span className={styles.muted}>
                {reconciled ? (
                  <>
                    {formatRate(totals.difference_percent)} от{' '}
                    {formatMoney(totals.quoted_reconciled)}
                  </>
                ) : (
                  'сверять пока нечего'
                )}
              </span>
            </div>
            <div className={styles.card}>
              <span className={styles.cardLabel}>Состояния</span>
              <span className={styles.states}>
                <span>Счёт больше: {totals.overcharged}</span>
                <span>Счёт меньше: {totals.undercharged}</span>
                <span>Сошлось: {totals.matched}</span>
                <span className={styles.muted}>Ждут счёта: {totals.awaiting}</span>
              </span>
            </div>
          </section>
        )
      })}

      {nothingReconciled && (
        <p className={styles.notice}>
          За период не пришло ни одного счёта, поэтому сверять нечего. Это <b>не</b> значит, что
          расхождений нет: сегодня фактическую стоимость сообщает при оформлении только часть
          перевозчиков, а загрузки счетов файлом ещё нет.
        </p>
      )}

      {data && data.carriers.length > 0 && (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <caption className={styles.caption}>
              По перевозчикам — только там, где счета уже приходили
            </caption>
            <thead>
              <tr>
                <th>Перевозчик</th>
                <th>Сверено</th>
                <th>Расчёт</th>
                <th>Счета</th>
                <th>Расхождение</th>
                <th>Доля</th>
              </tr>
            </thead>
            <tbody>
              {data.carriers.map((row) => (
                <tr key={`${row.carrier_id ?? 'none'}-${row.currency}`}>
                  <td className={styles.strong}>{row.carrier_name ?? '—'}</td>
                  <td>{row.reconciled}</td>
                  <td>{formatMoney(row.quoted)}</td>
                  <td>{formatMoney(row.actual)}</td>
                  <td className={TONE_CLASS[differenceTone(row.difference)]}>
                    {formatMoney(row.difference)}
                  </td>
                  <td className={TONE_CLASS[differenceTone(row.difference)]}>
                    {formatRate(row.difference_percent)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Отправление</th>
              <th>Создано</th>
              <th>Перевозчик</th>
              <th>Статус</th>
              <th>Расчёт</th>
              <th>Счёт</th>
              <th>Расхождение</th>
              <th>Сверка</th>
            </tr>
          </thead>
          <tbody>
            {items.map((line) => (
              <tr key={line.shipment_id}>
                <td>
                  <Link href={`/shipments/view?id=${line.shipment_id}`}>{line.number}</Link>
                </td>
                <td className={styles.muted}>{formatDateTime(line.created_at)}</td>
                <td>{line.carrier_name ?? '—'}</td>
                <td className={styles.muted}>
                  {SHIPMENT_STATUS_LABELS[line.status] ?? line.status}
                </td>
                <td>{line.quoted ? formatMoney(line.quoted) : '—'}</td>
                <td>{line.actual ? formatMoney(line.actual) : '—'}</td>
                <td className={TONE_CLASS[differenceTone(line.difference)]}>
                  {line.difference ? formatMoney(line.difference) : '—'}
                </td>
                <td>
                  <span className={`${styles.badge} ${STATE_CLASS[line.state] ?? ''}`}>
                    {STATE_LABELS[line.state]}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {!reconciliation.isLoading && items.length === 0 && (
          <p className={styles.empty}>
            {reconciliation.isError
              ? 'Сверка не загружена'
              : 'За выбранный период отправлений нет.'}
          </p>
        )}
      </div>

      {data && data.total > items.length ? (
        <p className={styles.hint}>
          Показаны первые {items.length} из {data.total} отправлений периода. Итоги в шапке
          посчитаны по всему периоду, а не по видимым строкам.
        </p>
      ) : null}
    </AppShell>
  )
}
