'use client'

import { useState } from 'react'
import { detailsToText, errorMessage, technicalDetails } from '@/lib/errorDetails'
import styles from './ErrorNote.module.css'

/**
 * Сообщение об ошибке с техническими подробностями для поддержки.
 *
 * Один компонент на все экраны намеренно. До него каждый рисовал ошибку
 * по-своему — `role="alert"` с текстом и без единого следа того, какой
 * это был запрос, — и идентификатор, который бэкенд протаскивает сквозь
 * весь стек, никуда не доходил (фронт-ТЗ, раздел 3).
 *
 * Подробности **свёрнуты**: оператору нужен текст ошибки, а код и
 * идентификатор — только когда он пишет в поддержку. Развёрнутые, они
 * превращали бы каждую ошибку в стену служебных строк, и текст в ней
 * терялся бы.
 */
export function ErrorNote({ error, className }: { error: unknown; className?: string }) {
  const [copied, setCopied] = useState(false)
  const details = technicalDetails(error)

  async function copy() {
    try {
      await navigator.clipboard.writeText(detailsToText(error))
      setCopied(true)
    } catch {
      // Буфера обмена может не быть вовсе: небезопасный контекст, отказ
      // в разрешении, старый браузер. Значения при этом видны на экране
      // и переписываются руками, поэтому падать здесь не за что.
      setCopied(false)
    }
  }

  return (
    <div role="alert" className={`${styles.note} ${className ?? ''}`}>
      <span className={styles.message}>{errorMessage(error)}</span>
      {details.length > 0 && (
        <details className={styles.details}>
          <summary>Технические подробности</summary>
          <dl className={styles.list}>
            {details.map((detail) => (
              <div key={detail.label} className={styles.row}>
                <dt>{detail.label}</dt>
                <dd>{detail.value}</dd>
              </div>
            ))}
          </dl>
          <button type="button" className={styles.copy} onClick={() => void copy()}>
            {copied ? 'Скопировано' : 'Скопировать для поддержки'}
          </button>
        </details>
      )}
    </div>
  )
}
