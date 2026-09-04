'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Link from 'next/link'
import { useRouter, useSearchParams } from 'next/navigation'
import { Suspense, useEffect } from 'react'
import { request, tokens, type ApiError, type BulkRun } from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { describeAddress } from '@/lib/bulkRows'
import { ROW_STATUS_LABELS, RUN_STATUS_LABELS, nextStep } from '@/lib/bulkStatus'
import { formatDateTime } from '@/lib/format'
import styles from '../page.module.css'

/** Порядок счётчиков: слева направо по ходу прогона. */
const COUNTER_ORDER = ['new', 'quoted', 'selected', 'created', 'failed'] as const

/**
 * Реестр массового расчёта. Идентификатор приходит параметром запроса, а не
 * сегментом пути: кабинет собирается статическим экспортом (ADR-0012),
 * а тот требует знать все значения динамического сегмента заранее —
 * идентификаторы же появляются в рантайме и у каждого тенанта свои.
 */
export default function BulkRunPage() {
  return (
    <Suspense fallback={null}>
      <BulkRunRegister />
    </Suspense>
  )
}

function BulkRunRegister() {
  const router = useRouter()
  const runId = useSearchParams().get('id') ?? ''
  const queryClient = useQueryClient()

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const run = useQuery({
    queryKey: ['bulk-run', runId],
    queryFn: () => request<BulkRun>(`/bulk-runs/${runId}`),
    enabled: Boolean(runId),
  })

  const advance = useMutation({
    mutationFn: (path: string) =>
      request<BulkRun>(`/bulk-runs/${runId}/${path}`, { method: 'POST' }),
    onSuccess: (updated) => {
      queryClient.setQueryData(['bulk-run', runId], updated)
      void queryClient.invalidateQueries({ queryKey: ['bulk-runs'] })
    },
  })

  const data = run.data
  const counts = data?.counts ?? {}
  const step = data ? nextStep(counts) : null
  const rows = data?.rows ?? []

  return (
    <AppShell>
      <div className={styles.head}>
        <h1>{data?.name ?? 'Массовый расчёт'}</h1>
        <Link href="/bulk">К списку</Link>
      </div>
      <p className={styles.hint}>
        {data ? (
          <>
            {RUN_STATUS_LABELS[data.status]} · отправитель:{' '}
            {describeAddress(data.sender_snapshot)} · создан {formatDateTime(data.created_at)}
          </>
        ) : (
          'Загружаем…'
        )}
      </p>

      {run.isError && (
        <div role="alert" className={styles.error}>
          {(run.error as ApiError).message}
        </div>
      )}

      <div className={styles.counters}>
        {COUNTER_ORDER.map((key) => (
          <div
            key={key}
            className={`${styles.counter} ${key === 'failed' && counts.failed ? (styles.counterFailed ?? '') : ''}`}
          >
            <div className={styles.counterValue}>{counts[key] ?? 0}</div>
            <div className={styles.counterLabel}>{ROW_STATUS_LABELS[key]}</div>
          </div>
        ))}
      </div>

      <div className={styles.row}>
        {step ? (
          <button
            type="button"
            onClick={() => advance.mutate(step.path)}
            disabled={advance.isPending}
          >
            {advance.isPending ? 'Выполняем…' : step.label}
          </button>
        ) : (
          <span className={styles.counterLabel}>
            {/* Прогон дошёл до конца: делать по нему больше нечего.
                Строки, которые не прошли, отсюда не переигрываются —
                это отдельный расчёт (ADR-0022). */}
            Прогон завершён: непройденных шагов не осталось.
          </span>
        )}
      </div>

      {advance.isError && (
        <div role="alert" className={styles.error}>
          {(advance.error as ApiError).message}
        </div>
      )}

      <p className={styles.hint}>
        Тариф по отдельной строке меняется обычным решением с заменой на экране «Расчёт и выбор»
        — отдельной кнопки здесь нет намеренно.
      </p>

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>№</th>
              <th>Получатель</th>
              <th>Состояние</th>
              <th>Отправление</th>
              <th>Причина</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>{row.position + 1}</td>
                <td>{describeAddress(row.recipient_snapshot)}</td>
                <td>
                  <span
                    className={`${styles.badge} ${
                      row.status === 'failed'
                        ? (styles.badgeFailed ?? '')
                        : row.status === 'created'
                          ? (styles.badgeDone ?? '')
                          : ''
                    }`}
                  >
                    {ROW_STATUS_LABELS[row.status]}
                  </span>
                </td>
                <td>
                  {row.shipment_id ? (
                    <Link href={`/shipments/view?id=${row.shipment_id}`}>Открыть</Link>
                  ) : (
                    '—'
                  )}
                </td>
                {/* Причина показывается как есть: оператор по ней решает,
                    чинить адрес или ждать перевозчика. */}
                <td className={styles.reason}>{row.error_message ?? ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!rows.length && !run.isLoading ? <p className={styles.empty}>Строк нет.</p> : null}
      </div>
    </AppShell>
  )
}
