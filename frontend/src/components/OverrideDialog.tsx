'use client'

import { useEffect, useRef, useState } from 'react'
import type { RateOffer } from '@/api/client'
import { formatMoney } from '@/lib/format'
import { OVERRIDE_REASONS } from '@/lib/overrideReason'
import styles from '@/app/rate-shopping/page.module.css'

interface Props {
  offer: RateOffer
  onCancel: () => void
  onConfirm: (reason: string, comment: string) => void
  submitting: boolean
}

export function OverrideDialog({ offer, onCancel, onConfirm, submitting }: Props) {
  const ref = useRef<HTMLDialogElement>(null)
  const [reason, setReason] = useState<string>('')
  const [comment, setComment] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    ref.current?.showModal()
  }, [])

  return (
    <dialog ref={ref} className={styles.dialog} onCancel={onCancel} aria-label="Причина выбора">
      <h2>Выбран не рекомендованный вариант</h2>
      <p className={styles.muted}>
        {offer.carrier_name ?? 'Перевозчик'}, {formatMoney(offer.total_cost)}. Причина попадёт в
        историю решений и в аналитику — укажите её честно.
      </p>

      <div className={styles.field}>
        <label htmlFor="override-reason">Причина</label>
        <select
          id="override-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        >
          <option value="">Выберите причину</option>
          {OVERRIDE_REASONS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        {error && <div style={{ color: 'var(--danger)', fontSize: 13 }}>{error}</div>}
      </div>

      <div className={styles.field}>
        <label htmlFor="override-comment">Комментарий (необязательно)</label>
        <input
          id="override-comment"
          value={comment}
          onChange={(event) => setComment(event.target.value)}
        />
      </div>

      <div className={styles.actions}>
        <button
          type="button"
          className={styles.primary}
          disabled={submitting}
          onClick={() => {
            if (!reason) {
              setError('Укажите причину')
              return
            }
            onConfirm(reason, comment)
          }}
        >
          {submitting ? 'Сохраняем…' : 'Подтвердить выбор'}
        </button>
        <button type="button" className={styles.secondary} onClick={onCancel}>
          Отмена
        </button>
      </div>
    </dialog>
  )
}
