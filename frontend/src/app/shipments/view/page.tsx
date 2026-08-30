'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Link from 'next/link'
import { useRouter, useSearchParams } from 'next/navigation'
import { Suspense, useEffect, useState } from 'react'
import {
  request,
  tokens,
  type ApiError,
  type Shipment,
  type TrackingEvent,
} from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { formatDateTime, formatMoney } from '@/lib/format'
import {
  EVENT_STATUS_LABELS,
  FINAL_STATUSES,
  SHIPMENT_STATUS_LABELS,
} from '@/lib/shipmentStatus'
import styles from './page.module.css'

/**
 * Карточка отправления. Идентификатор приходит параметром запроса, а не
 * сегментом пути: кабинет собирается статическим экспортом (ADR-0012),
 * а тот требует знать все значения динамического сегмента заранее —
 * идентификаторы же появляются в рантайме и у каждого тенанта свои.
 */
export default function ShipmentPage() {
  return (
    <Suspense fallback={null}>
      <ShipmentCard />
    </Suspense>
  )
}

function ShipmentCard() {
  const router = useRouter()
  const id = useSearchParams().get('id') ?? ''
  const client = useQueryClient()
  const [failure, setFailure] = useState<ApiError | null>(null)

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const shipment = useQuery({
    queryKey: ['shipment', id],
    queryFn: () => request<Shipment>(`/shipments/${id}`),
  })

  const timeline = useQuery({
    queryKey: ['tracking', id],
    queryFn: () => request<TrackingEvent[]>(`/shipments/${id}/tracking`),
  })

  const cancel = useMutation({
    mutationFn: () => request<Shipment>(`/shipments/${id}/cancel`, { method: 'POST' }),
    onMutate: () => setFailure(null),
    onSuccess: (updated) => {
      client.setQueryData(['shipment', id], updated)
      void client.invalidateQueries({ queryKey: ['shipments'] })
    },
    onError: (error) => setFailure(error as ApiError),
  })

  if (shipment.isError) {
    const error = shipment.error as ApiError
    return (
      <AppShell>
        <h1>Отправление</h1>
        <p role="alert">
          {error.status === 404 ? 'Отправление не найдено.' : error.message}
        </p>
        <Link href="/shipments">Ко всем отправлениям</Link>
      </AppShell>
    )
  }

  const data = shipment.data
  const late =
    data?.deadline && data.eta && new Date(data.eta) > new Date(data.deadline)
  // Отмена доступна, пока перевозчик её принимает. Финальные статусы кнопку
  // прячут: нажать её всё равно нельзя, а видеть недоступное действие
  // на карточке доставленного груза только сбивает.
  const cancellable = data !== undefined && !FINAL_STATUSES.has(data.status)

  return (
    <AppShell>
      <div className={styles.header}>
        <h1>{data?.number ?? 'Отправление'}</h1>
        {data && (
          <span>{SHIPMENT_STATUS_LABELS[data.status] ?? data.status}</span>
        )}
        {cancellable && (
          <button
            type="button"
            onClick={() => cancel.mutate()}
            disabled={cancel.isPending}
          >
            {cancel.isPending ? 'Отменяем…' : 'Отменить отправление'}
          </button>
        )}
        <Link href="/shipments">Ко всем отправлениям</Link>
      </div>

      {failure && (
        <p role="alert">
          {failure.message}
          {failure.requestId && <span className={styles.raw}> · {failure.requestId}</span>}
        </p>
      )}

      <div className={styles.grid}>
        <section className={styles.card}>
          <h2>Отправление</h2>
          {data ? (
            <dl className={styles.rows}>
              <dt>Перевозчик</dt>
              <dd>{data.carrier_name ?? '—'}</dd>
              <dt>Трек-номер</dt>
              <dd>{data.tracking_number ?? '—'}</dd>
              <dt>Номер у перевозчика</dt>
              <dd>{data.external_id ?? '—'}</dd>
              <dt>Ожидаемая доставка</dt>
              <dd>{formatDateTime(data.eta)}</dd>
              <dt>Крайний срок</dt>
              <dd className={late ? styles.late : undefined}>
                {formatDateTime(data.deadline)}
              </dd>
              <dt>Стоимость по расчёту</dt>
              <dd>{data.quoted_total_cost ? formatMoney(data.quoted_total_cost) : '—'}</dd>
              <dt>Фактическая стоимость</dt>
              {/* Расхождение с обещанной — предмет сверки счетов, поэтому
                  обещание не затирается фактом, а показывается рядом. */}
              <dd>{data.actual_total_cost ? formatMoney(data.actual_total_cost) : '—'}</dd>
              <dt>Создано</dt>
              <dd>{formatDateTime(data.created_at)}</dd>
            </dl>
          ) : (
            <p className={styles.empty}>Загружаем…</p>
          )}
        </section>

        <section className={styles.card}>
          <h2>Лента событий</h2>
          {timeline.data && timeline.data.length > 0 ? (
            <ol className={styles.timeline}>
              {/* Свежие сверху: оператор открывает карточку ради последнего
                  события, а не ради истории с начала. */}
              {[...timeline.data].reverse().map((event) => (
                <li key={`${event.occurred_at}-${event.carrier_status}`} className={styles.event}>
                  <div className={styles.eventStatus}>
                    {EVENT_STATUS_LABELS[event.normalized_status] ?? event.normalized_status}
                  </div>
                  <div className={styles.eventMeta}>
                    {formatDateTime(event.occurred_at)}
                    {event.location ? ` · ${event.location}` : ''}
                  </div>
                  {event.description && <div className={styles.eventMeta}>{event.description}</div>}
                  {/* Статус перевозчика показывается рядом с нашим: звонить
                      в службу поддержки ТК оператор будет на их языке. */}
                  {event.carrier_status && (
                    <div className={styles.raw}>у перевозчика: {event.carrier_status}</div>
                  )}
                </li>
              ))}
            </ol>
          ) : (
            <p className={styles.empty}>
              {timeline.isLoading
                ? 'Загружаем…'
                : 'Событий пока нет. Статусы подтягиваются по расписанию.'}
            </p>
          )}
        </section>
      </div>
    </AppShell>
  )
}
