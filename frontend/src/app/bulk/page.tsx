'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import {
  request,
  tokens,
  type ApiError,
  type BulkImport,
  type BulkImportRow,
  type BulkRun,
  type BulkRunPage,
} from '@/api/client'
import { AppShell } from '@/components/AppShell'
import {
  IMPORT_STATUS_LABELS,
  formatDestination,
  readyRows,
  resolvedDestination,
  type Choices,
} from '@/lib/bulkImport'
import { RUN_STATUS_LABELS, failedCount } from '@/lib/bulkStatus'
import { formatDateTime, formatMoney } from '@/lib/format'
import styles from './page.module.css'

/** Модули CSS типизированы как `string | undefined`: класса может не быть. */
const STATUS_CLASS: Record<BulkImportRow['status'], string | undefined> = {
  parsed: undefined,
  resolved: styles.badgeDone,
  ambiguous: undefined,
  not_found: styles.badgeFailed,
}

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
  const [preview, setPreview] = useState<BulkImport | null>(null)
  const [choices, setChoices] = useState<Choices>({})
  const [formError, setFormError] = useState<string | null>(null)

  const runs = useQuery({
    queryKey: ['bulk-runs'],
    queryFn: () => request<BulkRunPage>('/bulk-runs'),
  })

  const importList = useMutation({
    mutationFn: (text: string) =>
      request<BulkImport>('/bulk-runs/import', { method: 'POST', body: { text } }),
    onSuccess: (result) => {
      setPreview(result)
      setChoices({})
    },
    onError: (error: ApiError) => setFormError(error.message),
  })

  const create = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      request<BulkRun>('/bulk-runs', { method: 'POST', body }),
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ['bulk-runs'] })
      router.push(`/bulk/view?id=${run.id}`)
    },
    onError: (error: ApiError) => setFormError(error.message),
  })

  // Список изменился — предпросмотр устарел: показывать старый подбор
  // под новым текстом значит подставить не тех получателей.
  function changeRecipients(text: string) {
    setRecipients(text)
    setPreview(null)
    setChoices({})
  }

  async function pickFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    if (!file) return
    changeRecipients(await file.text())
    event.target.value = ''
  }

  function check() {
    setFormError(null)
    if (!recipients.trim()) {
      setFormError('Список получателей пуст')
      return
    }
    importList.mutate(recipients)
  }

  const cargo = {
    weightGrams: Number(weightGrams),
    cargoValue: { amount_minor: Number(valueMinor), currency: 'RUB' },
  }
  const ready = preview ? readyRows(preview, choices, cargo) : null

  function submit(event: React.FormEvent) {
    event.preventDefault()
    setFormError(null)
    if (!ready || !ready.rows.length) {
      setFormError('Сначала проверьте список: в прогон входят только готовые строки')
      return
    }
    create.mutate({
      name: name.trim() || null,
      origin: { country: 'RU', city: senderCity.trim(), address_line: senderAddress.trim() },
      strategy: 'optimal',
      rows: ready.rows.map((row) => ({
        destination: row.destination,
        packages: [
          { weight_grams: row.weight_grams, length_mm: 300, width_mm: 200, height_mm: 150 },
        ],
        cargo_value: row.cargo_value,
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

        <div className={styles.fileRow}>
          <label htmlFor="file">Файл списка (CSV, TSV или текст)</label>
          <input id="file" type="file" accept=".csv,.tsv,.txt,text/*" onChange={pickFile} />
        </div>

        <div className={styles.field}>
          <label htmlFor="recipients">Получатели</label>
          <textarea
            id="recipients"
            className={styles.textarea}
            value={recipients}
            onChange={(event) => changeRecipients(event.target.value)}
            placeholder={
              'Москва; ул. Получателя, 10\nВладивосток; ул. Примерная, 1\n\nили таблица с заголовком:\nИНН;Город;Вес\n7701234567;Тверь;1,5'
            }
          />
          <span className={styles.counterLabel}>
            По строке на получателя — «город; адрес». Либо таблица с заголовком: город, адрес,
            индекс, вес, ценность — или ИНН и название, тогда получатель подбирается по вашей
            адресной книге.
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
          <button type="button" onClick={check} disabled={importList.isPending}>
            {importList.isPending ? 'Проверяем…' : 'Проверить список'}
          </button>
          <button type="submit" disabled={create.isPending || !ready || !ready.rows.length}>
            {create.isPending
              ? 'Создаём…'
              : ready
                ? `Создать расчёт (${ready.rows.length})`
                : 'Создать расчёт'}
          </button>
        </div>

        <p className={styles.counterLabel}>
          Общий груз применяется к строкам, у которых в файле не назван свой вес или ценность.
        </p>
        {formError ? <p className={styles.error}>{formError}</p> : null}

        {preview ? (
          <div className={styles.preview}>
            <div className={styles.previewHead}>
              <h3>Проверка списка</h3>
              <span className={styles.counterLabel}>
                готово: {ready?.rows.length ?? 0}
                {ready?.excluded ? ` · не войдут: ${ready.excluded}` : ''}
                {preview.errors.length ? ` · не разобрано строк: ${preview.errors.length}` : ''}
              </span>
            </div>
            {preview.errors.length ? (
              <ul className={styles.errorList}>
                {preview.errors.map((error) => (
                  <li key={error}>{error}</li>
                ))}
              </ul>
            ) : null}
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>Строка</th>
                    <th>Результат</th>
                    <th>Искали</th>
                    <th>Адрес</th>
                    <th>Груз</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.map((row) => {
                    const destination = resolvedDestination(row, choices)
                    const options = row.match?.options ?? []
                    return (
                      <tr key={row.line}>
                        <td className={styles.lineNo}>{row.line}</td>
                        <td>
                          <span className={`${styles.badge} ${STATUS_CLASS[row.status] ?? ''}`}>
                            {IMPORT_STATUS_LABELS[row.status]}
                          </span>
                          {row.message ? (
                            <div className={styles.reason}>{row.message}</div>
                          ) : null}
                        </td>
                        <td>
                          {row.lookup ?? '—'}
                          {row.match ? (
                            <div className={styles.counterLabel}>
                              {row.match.counterparty_name}
                            </div>
                          ) : null}
                        </td>
                        <td>
                          {row.status === 'ambiguous' && options.length ? (
                            <select
                              className={styles.select}
                              value={choices[row.line] ?? ''}
                              onChange={(event) =>
                                setChoices({ ...choices, [row.line]: event.target.value })
                              }
                            >
                              <option value="">— выберите адрес —</option>
                              {options.map((option) => (
                                <option key={option.address_id} value={option.address_id}>
                                  {formatDestination(option.address)}
                                </option>
                              ))}
                            </select>
                          ) : destination ? (
                            formatDestination(destination)
                          ) : (
                            '—'
                          )}
                        </td>
                        <td className={styles.counterLabel}>
                          {row.weight_grams ?? cargo.weightGrams} г ·{' '}
                          {formatMoney(row.cargo_value ?? cargo.cargoValue)}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
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
