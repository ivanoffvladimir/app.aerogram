'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import { request, tokens, type ApiError, type BulkRun, type BulkRunPage } from '@/api/client'
import { AppShell } from '@/components/AppShell'
import { parseRecipients } from '@/lib/bulkRows'
import { RUN_STATUS_LABELS, failedCount } from '@/lib/bulkStatus'
import { formatDateTime } from '@/lib/format'
import styles from './page.module.css'

export default function BulkListPage() {
  const router = useRouter()
  const queryClient = useQueryClient()

  useEffect(() => {
    if (!tokens.access()) router.replace('/login')
  }, [router])

  const [name, setName] = useState('')
  const [senderCity, setSenderCity] = useState('')
  const [senderAddress, setSenderAddress] = useState('')
  const [recipients, setRecipients] = useState('')
  const [weightGrams, setWeightGrams] = useState('1000')
  const [valueMinor, setValueMinor] = useState('100000')
  const [formError, setFormError] = useState<string | null>(null)

  const runs = useQuery({
    queryKey: ['bulk-runs'],
    queryFn: () => request<BulkRunPage>('/bulk-runs'),
  })

  const create = useMutation({
    mutationFn: (body: unknown) =>
      request<BulkRun>('/bulk-runs', { method: 'POST', body: JSON.stringify(body) }),
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ['bulk-runs'] })
      router.push(`/bulk/view?id=${run.id}`)
    },
    onError: (error: ApiError) => setFormError(error.message),
  })

  function submit(event: React.FormEvent) {
    event.preventDefault()
    setFormError(null)
    const parsed = parseRecipients(recipients)
    if (parsed.errors.length) {
      setFormError(parsed.errors.join('; '))
      return
    }
    if (!parsed.rows.length) {
      setFormError('Список получателей пуст')
      return
    }
    create.mutate({
      name: name.trim() || null,
      origin: { country: 'RU', city: senderCity.trim(), address_line: senderAddress.trim() },
      strategy: 'optimal',
      rows: parsed.rows.map((row) => ({
        destination: { country: 'RU', city: row.city, address_line: row.addressLine },
        packages: [
          {
            weight_grams: Number(weightGrams),
            length_mm: 300,
            width_mm: 200,
            height_mm: 150,
          },
        ],
        cargo_value: { amount_minor: Number(valueMinor), currency: 'RUB' },
        cargo_type: 'parcel',
      })),
    })
  }

  const items = runs.data?.items ?? []

  return (
    <AppShell>
      <div className={styles.head}>
        <h1>Массовые отправления</h1>
      </div>
      <p className={styles.hint}>
        Один отправитель, много получателей. Расчёт и оформление идут списком, тариф подбирается
        на каждого получателя отдельно.
      </p>

      <form className={styles.panel} onSubmit={submit}>
        <h2>Новый массовый расчёт</h2>

        <div className={styles.row}>
          <div className={styles.field}>
            <label htmlFor="name">Название</label>
            <input
              id="name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="по умолчанию — сегодняшняя дата"
            />
          </div>
          <div className={styles.field}>
            <label htmlFor="sender-city">Город отправителя</label>
            <input
              id="sender-city"
              value={senderCity}
              onChange={(event) => setSenderCity(event.target.value)}
              required
            />
          </div>
          <div className={styles.field}>
            <label htmlFor="sender-address">Адрес отправителя</label>
            <input
              id="sender-address"
              value={senderAddress}
              onChange={(event) => setSenderAddress(event.target.value)}
              required
            />
          </div>
        </div>

        <div className={styles.field}>
          <label htmlFor="recipients">Получатели — по одному в строке, «город; адрес»</label>
          <textarea
            id="recipients"
            className={styles.textarea}
            value={recipients}
            onChange={(event) => setRecipients(event.target.value)}
            placeholder={'Москва; ул. Получателя, 10\nВладивосток; ул. Примерная, 1'}
            required
          />
          <span className={styles.counterLabel}>
            Импорт файла и подбор по адресной книге появятся следующим шагом.
          </span>
        </div>

        <div className={styles.row}>
          <div className={styles.field}>
            <label htmlFor="weight">Вес каждого места, г</label>
            <input
              id="weight"
              type="number"
              min="1"
              value={weightGrams}
              onChange={(event) => setWeightGrams(event.target.value)}
              required
            />
          </div>
          <div className={styles.field}>
            <label htmlFor="value">Объявленная стоимость, копейки</label>
            <input
              id="value"
              type="number"
              min="0"
              value={valueMinor}
              onChange={(event) => setValueMinor(event.target.value)}
              required
            />
          </div>
          <button type="submit" disabled={create.isPending}>
            {create.isPending ? 'Создаём…' : 'Создать'}
          </button>
        </div>

        <p className={styles.counterLabel}>
          Груз применяется ко всем получателям сразу. Отдельный груз по строке — следующим
          шагом.
        </p>
        {formError ? <p className={styles.error}>{formError}</p> : null}
      </form>

      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Название</th>
              <th>Состояние</th>
              <th>Строк</th>
              <th>Не прошло</th>
              <th>Создан</th>
            </tr>
          </thead>
          <tbody>
            {items.map((run) => {
              const total = Object.values(run.counts).reduce((sum, value) => sum + value, 0)
              const failed = failedCount(run.counts)
              return (
                <tr key={run.id}>
                  <td>
                    <Link href={`/bulk/view?id=${run.id}`}>{run.name}</Link>
                  </td>
                  <td>
                    <span className={styles.badge}>{RUN_STATUS_LABELS[run.status]}</span>
                  </td>
                  <td>{total}</td>
                  <td className={failed ? styles.reason : undefined}>{failed || '—'}</td>
                  <td>{formatDateTime(run.created_at)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {!items.length && !runs.isLoading ? (
          <p className={styles.empty}>Массовых расчётов пока нет.</p>
        ) : null}
      </div>
    </AppShell>
  )
}
