'use client'

import { useQuery } from '@tanstack/react-query'
import { useRouter } from 'next/navigation'
import { useEffect } from 'react'
import {
  request,
  tokens,
  type ApiError,
  type CarrierAnalytics,
  type CarrierConnection,
} from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { ACCOUNT_STATUS_LABELS, CARRIER_MODE_LABELS } from '@/lib/directory'
import { CONFIDENCE_LABELS, formatPercent } from '@/lib/format'
import styles from './page.module.css'

const SCOPE_LABELS: Record<string, string> = {
  global: 'по всем направлениям',
  direction: 'по направлению',
  direction_weight: 'по направлению и весу',
}

const CONFIDENCE_CLASS: Record<string, string | undefined> = {
  high: styles.confidenceHigh,
  medium: undefined,
  low: styles.confidenceLow,
  insufficient: styles.confidenceLow,
}

export default function CarriersPage() {
  const router = useRouter()

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const carriers = useQuery({
    queryKey: ['carrier-analytics'],
    queryFn: () => request<CarrierAnalytics[]>('/analytics/carriers'),
  })

  const connections = useQuery({
    queryKey: ['carrier-connections'],
    queryFn: () => request<CarrierConnection[]>('/carriers'),
  })

  return (
    <AppShell>
      <h1>Перевозчики</h1>

      <h2 className={styles.section}>Подключение</h2>
      <p className={styles.note}>
        По договору клиента считается его цена, по тарифу Logistics OS — наша. Учётные данные
        вводятся один раз и обратно не показываются: увидеть можно только то, какие поля нужны.
      </p>

      {connections.isError && <p role="alert">{(connections.error as ApiError).message}</p>}

      <div className={styles.tableWrap} style={{ marginBottom: 32 }}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Перевозчик</th>
              <th>Подключён</th>
              <th>Договор</th>
              <th>Доступы</th>
              <th>Делитель объёма</th>
              <th>Что нужно для подключения</th>
            </tr>
          </thead>
          <tbody>
            {(connections.data ?? []).map((carrier) => (
              <tr key={carrier.carrier_id}>
                <td>{carrier.name}</td>
                <td>{carrier.connected ? 'да' : <span className={styles.noData}>нет</span>}</td>
                <td>
                  {carrier.mode ? CARRIER_MODE_LABELS[carrier.mode] ?? carrier.mode : '—'}
                  {carrier.is_sandbox && <span className={styles.confidence}>песочница</span>}
                </td>
                <td>
                  {carrier.status
                    ? ACCOUNT_STATUS_LABELS[carrier.status] ?? carrier.status
                    : '—'}
                </td>
                {/* Делитель договорной: расхождение с договором должен
                    заметить человек, а не счёт от перевозчика. */}
                <td>{carrier.volumetric_divisor}</td>
                <td className={styles.components}>
                  {carrier.credential_fields.length === 0
                    ? 'состав доступов не определён'
                    : carrier.credential_fields
                        // Необязательное помечается: без секрета подписи
                        // перевозчик подключён и работает, просто на опросе.
                        .map((f) => (f.required ? f.label : `${f.label} (не обязателен)`))
                        .join(' · ')}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className={styles.section}>Качество доставки</h2>
      <p className={styles.note}>
        Скор считается по фактическим доставкам за последние 30 суток. Пока наблюдений меньше
        десяти, число не показывается вовсе: ноль читался бы как «худший перевозчик», а он
        всего лишь новый.
      </p>

      {carriers.isError && <p role="alert">{(carriers.error as ApiError).message}</p>}

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Перевозчик</th>
              <th>Скор</th>
              <th>Доверие</th>
              <th>Выборка</th>
              <th>Разрез</th>
              <th>Компоненты</th>
            </tr>
          </thead>
          <tbody>
            {(carriers.data ?? []).map((carrier) => (
              <tr key={carrier.carrier_id}>
                <td>{carrier.carrier_name}</td>
                <td>
                  {carrier.score === null ? (
                    <span className={styles.noData}>недостаточно данных</span>
                  ) : (
                    <span className={styles.score}>{carrier.score}</span>
                  )}
                </td>
                <td>
                  <span
                    className={`${styles.confidence} ${CONFIDENCE_CLASS[carrier.confidence] ?? ''}`}
                  >
                    {CONFIDENCE_LABELS[carrier.confidence] ?? carrier.confidence}
                  </span>
                </td>
                <td>{carrier.sample_size}</td>
                {/* Разрез показывается всегда: глобальный скор и скор
                    по направлению — разные утверждения (раздел 10.2 ТЗ). */}
                <td>
                  {carrier.scope_type ? SCOPE_LABELS[carrier.scope_type] ?? carrier.scope_type : '—'}
                </td>
                <td className={styles.components}>
                  {carrier.score === null
                    ? '—'
                    : [
                        `в срок ${formatPercent(carrier.components.on_time_rate)}`,
                        `надёжность ${formatPercent(carrier.components.reliability)}`,
                        `прозрачность ${formatPercent(carrier.components.data_quality)}`,
                      ].join(' · ')}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {!carriers.isLoading && (carriers.data?.length ?? 0) === 0 && (
          <p className={styles.note} style={{ padding: 24, margin: 0 }}>
            Перевозчики не подключены.
          </p>
        )}
      </div>
    </AppShell>
  )
}
