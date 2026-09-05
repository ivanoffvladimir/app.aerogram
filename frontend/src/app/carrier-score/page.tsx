'use client'

import { useQuery } from '@tanstack/react-query'
import { useRouter } from 'next/navigation'
import { useEffect } from 'react'
import { request, tokens, type ApiError, type CarrierAnalytics } from '@/api/client'
import { AppShell } from '@/components/AppShell'
import {
  COMPONENTS,
  SCOPE_LABELS,
  byScore,
  confidenceText,
  scoreText,
} from '@/lib/carrierScore'
import { formatDateTime, formatPercent } from '@/lib/format'
import styles from './page.module.css'

/** Модули CSS типизированы как `string | undefined`: класса может не быть. */
const CONFIDENCE_CLASS: Record<CarrierAnalytics['confidence'], string | undefined> = {
  high: styles.scoreHigh,
  medium: undefined,
  low: styles.scoreLow,
  insufficient: styles.scoreNone,
}

export default function CarrierScorePage() {
  const router = useRouter()

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const analytics = useQuery({
    queryKey: ['carrier-analytics'],
    queryFn: () => request<CarrierAnalytics[]>('/analytics/carriers'),
  })

  const rows = byScore(analytics.data ?? [])
  const formula = rows.find((row) => row.formula_version)?.formula_version

  return (
    <AppShell>
      <h1>Carrier Score</h1>
      <p className={styles.hint}>
        Скор считается <b>по вашим отправлениям</b>, а не по платформе целиком: у другого
        клиента с теми же перевозчиками он будет другим. Число само по себе ничего не значит —
        рядом стоит его расшифровка и то, на скольких отправлениях оно посчитано.
      </p>

      {analytics.isError && (
        <div role="alert" className={styles.empty}>
          {(analytics.error as ApiError).message}
        </div>
      )}

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Перевозчик</th>
              <th>Скор</th>
              <th>Доверие</th>
              {COMPONENTS.map((component) => (
                <th key={component.key}>{component.label}</th>
              ))}
              <th>Разрез</th>
              <th>Посчитан</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.carrier_id}>
                <td className={styles.carrier}>{row.carrier_name}</td>
                <td>
                  <span className={`${styles.score} ${CONFIDENCE_CLASS[row.confidence] ?? ''}`}>
                    {scoreText(row)}
                  </span>
                </td>
                <td className={styles.muted}>{confidenceText(row)}</td>
                {COMPONENTS.map((component) => {
                  const value = row.components[component.key]
                  return (
                    <td
                      key={component.key}
                      className={component.higherIsBetter ? undefined : styles.inverted}
                      // Единственная составляющая, где рост значения — ухудшение.
                      title={component.higherIsBetter ? undefined : 'Чем меньше, тем лучше'}
                    >
                      {formatPercent(value)}
                    </td>
                  )
                })}
                <td className={styles.muted}>
                  {row.scope_type ? (SCOPE_LABELS[row.scope_type] ?? row.scope_type) : '—'}
                  {row.scope_key ? (
                    <span className={styles.muted}> · {row.scope_key}</span>
                  ) : null}
                </td>
                <td className={styles.muted}>{formatDateTime(row.calculated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>

        {!analytics.isLoading && rows.length === 0 && (
          <p className={styles.empty}>
            {analytics.isError
              ? 'Список не загружен'
              : 'Подключённых перевозчиков пока нет — подключите их на экране «Перевозчики».'}
          </p>
        )}
      </div>

      {formula ? (
        <p className={styles.hint}>
          Формула: <code>{formula}</code>. Её версия меняется вместе с весами составляющих,
          иначе исторические оценки стали бы несопоставимыми.
        </p>
      ) : null}
    </AppShell>
  )
}
