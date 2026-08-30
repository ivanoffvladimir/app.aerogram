'use client'

import { useMutation } from '@tanstack/react-query'
import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import { useForm } from 'react-hook-form'
import {
  ApiError,
  newIdempotencyKey,
  request,
  tokens,
  type DecisionResponse,
  type RateOffer,
  type RateRequest,
  type RateResponse,
  type Recommendation,
} from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { OfferCard } from '@/components/OfferCard'
import { OverrideDialog } from '@/components/OverrideDialog'
import { CONFIDENCE_LABELS, formatDateTime, formatMoney } from '@/lib/format'
import styles from './page.module.css'

const STRATEGIES = [
  { value: 'optimal', label: 'Оптимальный' },
  { value: 'cheapest', label: 'Самый дешёвый' },
  { value: 'fastest', label: 'Самый быстрый' },
  { value: 'reliable', label: 'Самый надёжный' },
] as const

type Strategy = (typeof STRATEGIES)[number]['value']

interface FormValues {
  originCity: string
  originAddress: string
  destinationCity: string
  destinationAddress: string
  weightKg: string
  cargoValueRub: string
  deadline: string
  doorDelivery: boolean
  pickup: boolean
  insurance: boolean
}

export default function RateShoppingPage() {
  const router = useRouter()
  const [strategy, setStrategy] = useState<Strategy>('optimal')
  const [quote, setQuote] = useState<RateResponse | null>(null)
  const [recommendation, setRecommendation] = useState<Recommendation | null>(null)
  const [overriding, setOverriding] = useState<RateOffer | null>(null)
  const [decision, setDecision] = useState<DecisionResponse | null>(null)
  const [failure, setFailure] = useState<ApiError | null>(null)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  // Расчёт живёт минуты. Тикаем раз в секунду, чтобы кнопка выбора погасла
  // ровно тогда, когда выдача перестала быть действительной, а не после
  // отказа сервера (фронт-ТЗ, раздел 4: stale quote).
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])

  const { register, handleSubmit } = useForm<FormValues>({
    defaultValues: {
      originCity: 'Москва',
      originAddress: 'ул. Тверская, 1',
      destinationCity: 'Владивосток',
      destinationAddress: 'ул. Светланская, 10',
      weightKg: '12',
      cargoValueRub: '480000',
      deadline: '',
      doorDelivery: true,
      pickup: true,
      insurance: true,
    },
  })

  const rates = useMutation({
    mutationFn: async (values: FormValues) => {
      const services: string[] = []
      if (values.pickup) services.push('pickup')
      if (values.doorDelivery) services.push('door_delivery')
      if (values.insurance) services.push('insurance')

      const body: RateRequest = {
        origin: {
          country: 'RU',
          city: values.originCity,
          address_line: values.originAddress,
        },
        destination: {
          country: 'RU',
          city: values.destinationCity,
          address_line: values.destinationAddress,
        },
        packages: [{ weight_grams: Math.round(Number(values.weightKg) * 1000) }],
        // Рубли пользователя превращаются в копейки один раз, здесь,
        // и дальше ходят целым числом (фронт-ТЗ, раздел 10).
        cargo_value: {
          amount_minor: Math.round(Number(values.cargoValueRub) * 100),
          currency: 'RUB',
        },
        additional_services: services,
        strategy,
        ...(values.deadline ? { deadline: new Date(values.deadline).toISOString() } : {}),
      }
      return request<RateResponse>('/rates', { method: 'POST', body })
    },
    onSuccess: (data) => {
      setQuote(data)
      setRecommendation(null)
      setDecision(null)
      setFailure(null)
      recommend.mutate({ quoteId: data.quote_id, strategy })
    },
    onError: (error) => setFailure(error instanceof ApiError ? error : null),
  })

  const recommend = useMutation({
    mutationFn: ({ quoteId, strategy: chosen }: { quoteId: string; strategy: Strategy }) =>
      // Фронт не считает рекомендацию сам: источник истины — бэкенд
      // (фронт-ТЗ, раздел 5).
      request<Recommendation>('/routing/quote', {
        method: 'POST',
        body: { quote_id: quoteId, strategy: chosen },
      }),
    onSuccess: setRecommendation,
    onError: (error) => setFailure(error instanceof ApiError ? error : null),
  })

  const decide = useMutation({
    mutationFn: ({
      offerId,
      reason,
      comment,
    }: {
      offerId: string
      reason?: string
      comment?: string
    }) => {
      if (!recommendation) throw new Error('нет рекомендации')
      const isOverride = offerId !== recommendation.recommended_offer_id
      return request<DecisionResponse>('/decisions', {
        method: 'POST',
        // Ключ на попытку: повтор после обрыва обязан прислать тот же,
        // иначе создастся второе решение.
        idempotencyKey: newIdempotencyKey(),
        body: {
          recommendation_id: recommendation.id,
          selected_offer_id: offerId,
          override: isOverride,
          ...(isOverride && reason ? { override_reason: reason } : {}),
          ...(isOverride && comment ? { override_comment: comment } : {}),
          mode: 'manual',
        },
      })
    },
    onSuccess: (data) => {
      setDecision(data)
      setOverriding(null)
    },
    onError: (error) => {
      setFailure(error instanceof ApiError ? error : null)
      setOverriding(null)
    },
  })

  // valid_until в контракте необязателен: если срок не пришёл, считать выдачу
  // просроченной нельзя — это заблокировало бы выбор без причины.
  const isStale = quote?.valid_until ? new Date(quote.valid_until).getTime() <= now : false
  const offers = quote?.offers ?? []
  const recommended = offers.find((offer) => offer.id === recommendation?.recommended_offer_id)
  const alternatives = offers.filter((offer) => offer.id !== recommended?.id && offer.eligible)
  const rejected = offers.filter((offer) => !offer.eligible)

  function selectOffer(offer: RateOffer) {
    if (!recommendation) return
    if (offer.id === recommendation.recommended_offer_id) {
      decide.mutate({ offerId: offer.id })
      return
    }
    setOverriding(offer)
  }

  return (
    <AppShell>
      <div className={styles.header}>
        <h1>Расчёт и выбор перевозчика</h1>
      </div>

      <form
        className={styles.card}
        method="post"
        onSubmit={handleSubmit((values) => rates.mutate(values))}
        noValidate
      >
        <div className={styles.grid}>
          <div>
            <label htmlFor="originCity">Город отправления</label>
            <input id="originCity" {...register('originCity')} />
          </div>
          <div>
            <label htmlFor="originAddress">Адрес отправления</label>
            <input id="originAddress" {...register('originAddress')} />
          </div>
          <div>
            <label htmlFor="destinationCity">Город назначения</label>
            <input id="destinationCity" {...register('destinationCity')} />
          </div>
          <div>
            <label htmlFor="destinationAddress">Адрес назначения</label>
            <input id="destinationAddress" {...register('destinationAddress')} />
          </div>
          <div>
            <label htmlFor="weightKg">Вес, кг</label>
            <input id="weightKg" type="number" step="0.001" {...register('weightKg')} />
          </div>
          <div>
            <label htmlFor="cargoValueRub">Стоимость груза, ₽</label>
            <input id="cargoValueRub" type="number" step="0.01" {...register('cargoValueRub')} />
          </div>
          <div>
            <label htmlFor="deadline">Крайний срок доставки</label>
            <input id="deadline" type="datetime-local" {...register('deadline')} />
          </div>
        </div>

        <div className={styles.actions}>
          <label style={{ display: 'inline-flex', gap: 6, alignItems: 'center', margin: 0 }}>
            <input type="checkbox" style={{ width: 'auto' }} {...register('pickup')} /> Забор груза
          </label>
          <label style={{ display: 'inline-flex', gap: 6, alignItems: 'center', margin: 0 }}>
            <input type="checkbox" style={{ width: 'auto' }} {...register('doorDelivery')} />
            До двери
          </label>
          <label style={{ display: 'inline-flex', gap: 6, alignItems: 'center', margin: 0 }}>
            <input type="checkbox" style={{ width: 'auto' }} {...register('insurance')} />
            Страхование
          </label>
          <button type="submit" className={styles.primary} disabled={rates.isPending}>
            {rates.isPending ? 'Считаем…' : 'Рассчитать'}
          </button>
        </div>
      </form>

      {failure && (
        <div className={styles.danger} role="alert">
          {failure.message}
          {failure.requestId && (
            <div className={styles.requestId}>Идентификатор запроса: {failure.requestId}</div>
          )}
        </div>
      )}

      {rates.isPending && <div className={styles.skeleton} />}

      {quote && (
        <>
          <div className={styles.tabs}>
            {STRATEGIES.map((item) => (
              <button
                key={item.value}
                type="button"
                className={`${styles.tab} ${strategy === item.value ? styles.tabActive : ''}`}
                onClick={() => {
                  setStrategy(item.value)
                  recommend.mutate({ quoteId: quote.quote_id, strategy: item.value })
                }}
              >
                {item.label}
              </button>
            ))}
          </div>

          {quote.failures.length > 0 && (
            <div className={styles.warning}>
              {/* Показывается сообщение перевозчика, а не машинный код: код
                  оператору ничего не говорит, а решение принимать ему. */}
              Часть перевозчиков не ответила:{' '}
              {quote.failures.map((f) => f.message).join('; ')}.
              {offers.length > 0 && ' Остальные варианты доступны для выбора.'}
            </div>
          )}

          {quote.no_deadline_match && (
            <div className={styles.danger}>
              В указанный срок не укладывается ни один перевозчик. Ниже — ближайшие альтернативы.
            </div>
          )}

          {isStale && (
            <div className={styles.warning}>
              Расчёт устарел. Выбор недоступен — нужно пересчитать.
            </div>
          )}

          {decision && (
            <div className={styles.card} style={{ borderColor: 'var(--success)' }}>
              <strong>Решение зафиксировано.</strong> Снимок {decision.snapshot_id.slice(0, 8)},
              решение {decision.decision_id.slice(0, 8)} от {formatDateTime(decision.created_at)}.
            </div>
          )}

          {recommend.isPending && <div className={styles.skeleton} />}

          {recommendation && recommended && (
            <section className={styles.hero}>
              <div className={styles.heroTop}>
                <div>
                  <span className={styles.badge}>Рекомендуем</span>
                  <h2 style={{ margin: '8px 0 0' }}>
                    {recommended.carrier_name ?? 'Перевозчик'} —{' '}
                    {recommended.service_name ?? recommended.service_code}
                  </h2>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <div className={styles.price}>{formatMoney(recommended.total_cost)}</div>
                  <div className={styles.muted}>{formatDateTime(recommended.eta)}</div>
                </div>
              </div>

              <ul className={styles.explanation}>
                {recommendation.explanation?.map((line) => <li key={line}>{line}</li>)}
              </ul>

              <div className={styles.actions}>
                <button
                  type="button"
                  className={styles.primary}
                  disabled={isStale || decide.isPending || Boolean(decision)}
                  onClick={() => selectOffer(recommended)}
                >
                  Принять рекомендацию
                </button>
                <span className={styles.muted}>
                  Уверенность:{' '}
                  {recommendation.confidence
                    ? (CONFIDENCE_LABELS[recommendation.confidence] ??
                      recommendation.confidence)
                    : '—'}{' '}
                  · формула {recommendation.algorithm_version} · политика{' '}
                  {recommendation.policy_version}
                </span>
              </div>
            </section>
          )}

          {recommendation && !recommended && (
            <div className={styles.card}>
              <strong>Рекомендации нет.</strong>{' '}
              <span className={styles.muted}>
                {recommendation.explanation?.[0] ?? 'Подходящих вариантов нет'}
              </span>
            </div>
          )}

          {alternatives.length > 0 && <h2>Другие подходящие варианты</h2>}
          {alternatives.map((offer) => (
            <OfferCard
              key={offer.id}
              offer={offer}
              onSelect={selectOffer}
              selectDisabled={isStale || decide.isPending || Boolean(decision) || !recommendation}
            />
          ))}

          {rejected.length > 0 && <h2>Не подходят под ограничения</h2>}
          {rejected.map((offer) => (
            <OfferCard key={offer.id} offer={offer} />
          ))}

          {offers.length === 0 && quote.failures.length === 0 && (
            <div className={styles.card}>
              Ни один перевозчик не вернул предложений. Проверьте подключённые договоры.
            </div>
          )}
        </>
      )}

      {overriding && (
        <OverrideDialog
          offer={overriding}
          submitting={decide.isPending}
          onCancel={() => setOverriding(null)}
          onConfirm={(reason, comment) =>
            decide.mutate({ offerId: overriding.id, reason, comment })
          }
        />
      )}
    </AppShell>
  )
}
