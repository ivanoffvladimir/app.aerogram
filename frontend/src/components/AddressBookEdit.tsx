'use client'

import { useState } from 'react'
import type { Counterparty, CounterpartyAddress } from '@/api/client'
import { changedFields } from '@/lib/patch'
import styles from './AddressBookEdit.module.css'

/** Поля контрагента, которые правятся. ИНН и КПП сюда не входят: это
 *  не описка в названии, а другая организация. */
type ContactDraft = Record<'name' | 'contact_person' | 'phone' | 'email', string | null>

/** Поля адреса. Город и его код ФИАС не правятся руками: город приходит
 *  из подсказки, а набранный вручную не сопоставится с перевозчиком. */
type AddressDraft = Record<
  'label' | 'street' | 'house' | 'flat' | 'postal_code',
  string | null
> & { is_default_sender: boolean }

function contactsOf(counterparty: Counterparty): ContactDraft {
  return {
    name: counterparty.name,
    contact_person: counterparty.contact_person,
    phone: counterparty.phone,
    email: counterparty.email,
  }
}

function addressOf(address: CounterpartyAddress): AddressDraft {
  return {
    label: address.label,
    street: address.street,
    house: address.house,
    flat: address.flat,
    postal_code: address.postal_code,
    is_default_sender: address.is_default_sender,
  }
}

interface FieldsProps {
  draft: Record<string, string | boolean | null>
  labels: Record<string, string>
  onChange: (field: string, value: string) => void
  prefix: string
}

function Fields({ draft, labels, onChange, prefix }: FieldsProps) {
  return (
    <>
      {Object.entries(labels).map(([field, label]) => (
        <div key={field} className={styles.field}>
          <label htmlFor={`${prefix}-${field}`}>{label}</label>
          <input
            id={`${prefix}-${field}`}
            value={String(draft[field] ?? '')}
            onChange={(event) => onChange(field, event.target.value)}
          />
        </div>
      ))}
    </>
  )
}

const CONTACT_LABELS = {
  name: 'Название',
  contact_person: 'Контактное лицо',
  phone: 'Телефон',
  email: 'Почта',
}

const ADDRESS_LABELS = {
  label: 'Метка',
  street: 'Улица',
  house: 'Дом',
  flat: 'Квартира',
  postal_code: 'Индекс',
}

interface Props {
  counterparty: Counterparty
  pending: boolean
  error: string | null
  onSaveContacts: (patch: Record<string, unknown>) => void
  onSaveAddress: (addressId: string, patch: Record<string, unknown>) => void
  onDone: () => void
}

/**
 * Правка контрагента и его адресов.
 *
 * Уходят только изменённые поля (`changedFields`): форма целиком затёрла бы
 * чужую правку из соседней вкладки значениями, которых оператор не касался.
 * Пустое поле означает очистку, и сервер понимает это как `null`.
 *
 * Правка **не касается созданных отправлений**: у них снимок адреса
 * на момент создания, а не ссылка на строку адресной книги.
 */
export function AddressBookEdit({
  counterparty,
  pending,
  error,
  onSaveContacts,
  onSaveAddress,
  onDone,
}: Props) {
  const [contacts, setContacts] = useState<ContactDraft>(() => contactsOf(counterparty))
  const [addresses, setAddresses] = useState<Record<string, AddressDraft>>(() =>
    Object.fromEntries(counterparty.addresses.map((a) => [a.id, addressOf(a)])),
  )
  const [nothing, setNothing] = useState(false)

  function submit(event: React.FormEvent) {
    event.preventDefault()
    setNothing(false)
    const contactPatch = changedFields(contactsOf(counterparty), contacts)
    const addressPatches = counterparty.addresses
      .map((address) => ({
        id: address.id,
        patch: changedFields(addressOf(address), addresses[address.id] ?? addressOf(address)),
      }))
      .filter((entry) => Object.keys(entry.patch).length > 0)

    if (!Object.keys(contactPatch).length && !addressPatches.length) {
      // Пустой запрос трогает `updated_at` и аудит без единой правки.
      setNothing(true)
      return
    }
    if (Object.keys(contactPatch).length) onSaveContacts(contactPatch)
    for (const entry of addressPatches) onSaveAddress(entry.id, entry.patch)
  }

  return (
    <form className={styles.panel} onSubmit={submit}>
      <h3>Правка: {counterparty.name}</h3>
      <p className={styles.hint}>
        ИНН и КПП не меняются — другой ИНН означает другого контрагента. Уже созданные
        отправления правка не затрагивает: в них лежит снимок адреса.
      </p>

      <div className={styles.row}>
        <Fields
          draft={contacts}
          labels={CONTACT_LABELS}
          prefix="contact"
          onChange={(field, value) => setContacts({ ...contacts, [field]: value })}
        />
      </div>

      {counterparty.addresses.map((address) => {
        const draft = addresses[address.id] ?? addressOf(address)
        return (
          <fieldset key={address.id} className={styles.address}>
            <legend>{address.city}</legend>
            <div className={styles.row}>
              <Fields
                draft={draft}
                labels={ADDRESS_LABELS}
                prefix={`address-${address.id}`}
                onChange={(field, value) =>
                  setAddresses({ ...addresses, [address.id]: { ...draft, [field]: value } })
                }
              />
              <label className={styles.check}>
                <input
                  type="checkbox"
                  checked={draft.is_default_sender}
                  onChange={(event) =>
                    setAddresses({
                      ...addresses,
                      [address.id]: { ...draft, is_default_sender: event.target.checked },
                    })
                  }
                />
                Адрес отправителя по умолчанию
              </label>
            </div>
          </fieldset>
        )
      })}

      <div className={styles.row}>
        <button type="submit" disabled={pending}>
          {pending ? 'Сохраняем…' : 'Сохранить'}
        </button>
        <button type="button" onClick={onDone}>
          Отмена
        </button>
      </div>
      {nothing ? <p className={styles.hint}>Ничего не изменилось.</p> : null}
      {error ? (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      ) : null}
    </form>
  )
}
