'use client'

import { useState } from 'react'
import type { RateOffer } from '@/api/client'
import {
  COST_COMPONENT_LABELS,
  INELIGIBILITY_LABELS,
  RISK_LABELS,
  SOURCE_LABELS,
  formatDateTime,
  formatDuration,
  formatMoney,
  formatPercent,
} from '@/lib/format'
import styles from '@/app/rate-shopping/page.module.css'

interface Props {
  offer: RateOffer
  onSelect?: (offer: RateOffer) => void
  selectDisabled?: boolean
}

/**
 * Строка выдачи. Непригодное предложение не скрывается, а показывается
 * приглушённым с причиной (продуктовое ТЗ, раздел 7): оператор должен видеть,
 * что вариант есть и почему он не подходит.
 */
export function OfferCard({ offer, onSelect, selectDisabled }: Props) {
  const [showBreakdown, setShowBreakdown] = useState(false)

  return (
    <div className={`${styles.card} ${offer.eligible ? '' : styles.ineligible}`}>
      <div className={styles.heroTop}>
        <div>
          <strong>{offer.carrier_name ?? 'Перевозчик'}</strong>{' '}
          <span className={styles.muted}>{offer.service_name ?? offer.service_code}</span>
          <div>
            {offer.source && <span className={styles.badge}>{SOURCE_LABELS[offer.source]}</span>}
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div className={styles.price}>{formatMoney(offer.total_cost)}</div>
          <div className={styles.muted}>Срок: {formatDateTime(offer.eta)}</div>
        </div>
      </div>

      {!offer.eligible && (
        <p className={styles.danger} style={{ marginTop: 12, marginBottom: 0 }}>
          {offer.ineligibility_reason
            ? (INELIGIBILITY_LABELS[offer.ineligibility_reason] ?? offer.ineligibility_reason)
            : 'Вариант не подходит'}
          {offer.lateness_seconds ? ` — опоздание ${formatDuration(offer.lateness_seconds)}` : ''}
        </p>
      )}

      <div className={styles.metrics}>
        {offer.deadline_margin_seconds !== null &&
          offer.deadline_margin_seconds !== undefined && (
            <div className={styles.metric}>
              <span>Запас до срока</span>
              {formatDuration(offer.deadline_margin_seconds)}
            </div>
          )}
        <div className={styles.metric}>
          <span>Вероятность в срок</span>
          {formatPercent(offer.on_time_probability)}
        </div>
        <div className={styles.metric}>
          <span>Carrier Score</span>
          {offer.carrier_score ?? '—'}
        </div>
        <div className={styles.metric}>
          <span>Риск</span>
          {offer.risk ? (RISK_LABELS[offer.risk] ?? offer.risk) : '—'}
        </div>
      </div>

      <div className={styles.actions} style={{ marginTop: 0 }}>
        {offer.cost_components && offer.cost_components.length > 0 && (
          <button
            type="button"
            className={styles.rowButton}
            onClick={() => setShowBreakdown((open) => !open)}
            aria-expanded={showBreakdown}
          >
            {showBreakdown ? 'Скрыть расшифровку' : 'Расшифровка стоимости'}
          </button>
        )}
        {onSelect && (
          <button
            type="button"
            className={styles.rowButton}
            disabled={selectDisabled || !offer.eligible}
            onClick={() => onSelect(offer)}
          >
            Выбрать этот вариант
          </button>
        )}
      </div>

      {showBreakdown && offer.cost_components && (
        <div className={styles.breakdown}>
          <table>
            <tbody>
              {offer.cost_components.map((component, index) => (
                <tr key={`${component.type}-${index}`}>
                  <td>
                    {COST_COMPONENT_LABELS[component.type] ?? component.type}
                    {component.rate_percent ? ` (${component.rate_percent}%)` : ''}
                  </td>
                  <td style={{ textAlign: 'right' }}>{formatMoney(component.money)}</td>
                </tr>
              ))}
              <tr>
                <td>
                  <strong>Итого</strong>
                </td>
                <td style={{ textAlign: 'right' }}>
                  <strong>{formatMoney(offer.total_cost)}</strong>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
