'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Link from 'next/link'
import { useRouter, useSearchParams } from 'next/navigation'
import { Suspense, useEffect, useState } from 'react'
import {
  download,
  request,
  tokens,
  type ApiError,
  type Decision,
  type Shipment,
  type ShipmentDocument,
  type TrackingEvent,
} from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { ErrorNote } from '@/components/ErrorNote'
import { decisionMaker, overrideSummary } from '@/lib/decision'
import { documentState, documentTitle, isDownloadable } from '@/lib/documentStatus'
import { formatDateTime, formatMoney } from '@/lib/format'
import { SELECTION_LABELS } from '@/lib/routingRules'
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
  const [failure, setFailure] = useState<unknown>(null)

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

  //: Решение, по которому создано отправление. Отдельным запросом, потому что
  //  идентификатор решения приходит в самом отправлении, а его снимок —
  //  единственное место, где написано, чем объясняется выбор перевозчика.
  //  `enabled` держит запрос до появления идентификатора: у отправления,
  //  заведённого без решения, его нет вовсе.
  const decisionId = shipment.data?.decision_id ?? null
  const decision = useQuery({
    queryKey: ['decision', decisionId],
    queryFn: () => request<Decision>(`/decisions/${decisionId}`),
    enabled: Boolean(decisionId),
  })

  const documents = useQuery({
    queryKey: ['documents', id],
    queryFn: () => request<ShipmentDocument[]>(`/shipments/${id}/documents`),
  })

  //: Заказ формы стоит вызова у перевозчика, а у Почты России — суточной
  //  квоты. Кнопка поэтому одна и гаснет на время запроса: повторное
  //  нажатие ничего не ускорит, а квоту потратит.
  const orderLabel = useMutation({
    mutationFn: () =>
      request<ShipmentDocument>(`/shipments/${id}/documents`, { method: 'POST', body: {} }),
    onMutate: () => setFailure(null),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['documents', id] }),
    onError: (error) => setFailure(error),
  })

  const save = useMutation({
    mutationFn: (document: ShipmentDocument) =>
      download(`/documents/${document.id}/content`, `${document.type}.${document.format}`),
    onMutate: () => setFailure(null),
    onError: (error) => setFailure(error),
  })

  const cancel = useMutation({
    mutationFn: () => request<Shipment>(`/shipments/${id}/cancel`, { method: 'POST' }),
    onMutate: () => setFailure(null),
    onSuccess: (updated) => {
      client.setQueryData(['shipment', id], updated)
      void client.invalidateQueries({ queryKey: ['shipments'] })
    },
    onError: (error) => setFailure(error),
  })

  if (shipment.isError) {
    const error = shipment.error as ApiError
    return (
      <AppShell>
        <h1>Отправление</h1>
        <p role="alert">{error.status === 404 ? 'Отправление не найдено.' : error.message}</p>
        <Link href="/shipments">Ко всем отправлениям</Link>
      </AppShell>
    )
  }

  const data = shipment.data
  const late = data?.deadline && data.eta && new Date(data.eta) > new Date(data.deadline)
  // Отмена доступна, пока перевозчик её принимает. Финальные статусы кнопку
  // прячут: нажать её всё равно нельзя, а видеть недоступное действие
  // на карточке доставленного груза только сбивает.
  const cancellable = data !== undefined && !FINAL_STATUSES.has(data.status)

  return (
    <AppShell>
      <div className={styles.header}>
        <h1>{data?.number ?? 'Отправление'}</h1>
        {data && <span>{SHIPMENT_STATUS_LABELS[data.status] ?? data.status}</span>}
        {cancellable && (
          <button type="button" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
            {cancel.isPending ? 'Отменяем…' : 'Отменить отправление'}
          </button>
        )}
        <Link href="/shipments">Ко всем отправлениям</Link>
      </div>

      {failure ? <ErrorNote error={failure} /> : null}

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
              {/* Накладная и заказ нумеруются по-разному: у Деловых Линий
                  это два разных значения, и в споре предъявляют накладную. */}
              <dt>Номер накладной</dt>
              <dd>{data.waybill_number ?? '—'}</dd>
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
          <h2>Решение</h2>
          {decision.data ? (
            <dl className={styles.rows}>
              {/* «Человек», «правило» и «интеграция» — три разных ответа
                  на один и тот же вопрос спора с клиентом. Разбор живёт
                  в lib и проверен тестом: это правило, а не разметка. */}
              <dt>Кто выбрал</dt>
              <dd>{decisionMaker(decision.data)}</dd>
              {decision.data.selection_rule && (
                <>
                  <dt>Правило автовыбора</dt>
                  <dd>
                    {SELECTION_LABELS[decision.data.selection_rule] ??
                      decision.data.selection_rule}
                  </dd>
                </>
              )}
              <dt>Отказ от рекомендации</dt>
              <dd>{overrideSummary(decision.data)}</dd>
              {decision.data.override_comment && (
                <>
                  <dt>Комментарий</dt>
                  <dd>{decision.data.override_comment}</dd>
                </>
              )}
              <dt>Принято</dt>
              <dd>{formatDateTime(decision.data.decided_at)}</dd>
              {decision.data.selection_version && (
                <>
                  <dt>Версия выбора</dt>
                  <dd>{decision.data.selection_version}</dd>
                </>
              )}
            </dl>
          ) : (
            <p className={styles.empty}>
              {/* Отправление без решения — законное состояние: так заводятся
                  «призраки», найденные сверкой у перевозчика. */}
              {decisionId
                ? decision.isLoading
                  ? 'Загружаем…'
                  : 'Решение недоступно'
                : 'Отправление заведено без решения Decision Engine'}
            </p>
          )}
        </section>

        <section className={styles.card}>
          <h2>Документы</h2>
          {(documents.data ?? []).length > 0 ? (
            <ul className={styles.rows} style={{ display: 'block', margin: 0, padding: 0 }}>
              {(documents.data ?? []).map((document) => (
                <li key={document.id} style={{ listStyle: 'none', marginBottom: 12 }}>
                  <div>{documentTitle(document)}</div>
                  <div className={styles.eventMeta}>{documentState(document)}</div>
                  {isDownloadable(document) && (
                    <button
                      type="button"
                      onClick={() => save.mutate(document)}
                      disabled={save.isPending}
                    >
                      {save.isPending ? 'Скачиваем…' : 'Скачать'}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className={styles.empty}>
              {documents.isLoading ? 'Загружаем…' : 'Печатных форм пока нет'}
            </p>
          )}
          {/* Заказ формы предлагается, пока её нет: повторное обращение
              к перевозчику ничего не даёт, а квоту тратит. */}
          {!documents.isLoading && (documents.data ?? []).length === 0 && (
            <button
              type="button"
              onClick={() => orderLabel.mutate()}
              disabled={orderLabel.isPending}
            >
              {orderLabel.isPending ? 'Запрашиваем у перевозчика…' : 'Запросить этикетку'}
            </button>
          )}
        </section>

        <section className={styles.card}>
          <h2>Лента событий</h2>
          {timeline.data && timeline.data.length > 0 ? (
            <ol className={styles.timeline}>
              {/* Свежие сверху: оператор открывает карточку ради последнего
                  события, а не ради истории с начала. */}
              {[...timeline.data].reverse().map((event) => (
                <li
                  key={`${event.occurred_at}-${event.carrier_status}`}
                  className={styles.event}
                >
                  <div className={styles.eventStatus}>
                    {EVENT_STATUS_LABELS[event.normalized_status] ?? event.normalized_status}
                  </div>
                  <div className={styles.eventMeta}>
                    {formatDateTime(event.occurred_at)}
                    {event.location ? ` · ${event.location}` : ''}
                  </div>
                  {event.description && (
                    <div className={styles.eventMeta}>{event.description}</div>
                  )}
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
