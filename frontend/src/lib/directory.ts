/** Словари адресной книги и пользователей. Значения совпадают с бэкендом. */

/**
 * Тип контрагента. Ключи — значения проверки `counterparty_type`
 * в `core/models.py`; разойдись они, в таблице появилось бы английское слово.
 */
export const COUNTERPARTY_TYPE_LABELS: Record<string, string> = {
  legal: 'Юридическое лицо',
  individual: 'Физическое лицо',
  entrepreneur: 'ИП',
}

/**
 * Роли, которые владелец тенанта вправе выдать.
 *
 * Это ``TenantRole`` бэкенда, а не ``UserRole``: платформенных ролей здесь
 * нет физически, и предложить их в форме нельзя — как нельзя и на сервере.
 */
export const TENANT_ROLES = ['owner', 'logistician', 'operator'] as const

export type TenantRole = (typeof TENANT_ROLES)[number]

/** Подписи ролей. Включая ``viewer`` и ``api_client``: выдать их нельзя,
 *  но у существующего пользователя такая роль встречается, и показать её надо. */
export const ROLE_LABELS: Record<string, string> = {
  owner: 'Владелец',
  logistician: 'Логист',
  operator: 'Оператор',
  viewer: 'Наблюдатель',
  api_client: 'Машинный клиент',
  platform_admin: 'Администратор платформы',
  support: 'Поддержка',
}

/** Адрес одной строкой: город обязателен, остальное — по наличию. */
export function formatAddress(address: {
  region: string | null
  city: string
  street: string | null
  house: string | null
  flat: string | null
  postal_code: string | null
}): string {
  return [
    address.postal_code,
    address.region,
    address.city,
    address.street,
    address.house,
    address.flat === null ? null : `кв. ${address.flat}`,
  ]
    .filter((part): part is string => Boolean(part))
    .join(', ')
}


/** Режим договора с перевозчиком. */
export const CARRIER_MODE_LABELS: Record<string, string> = {
  own_contract: 'Договор клиента',
  aerogram: 'Тариф Logistics OS',
}

/** Итог последней проверки доступов. */
export const ACCOUNT_STATUS_LABELS: Record<string, string> = {
  unchecked: 'не проверялись',
  ok: 'в порядке',
  error: 'ошибка',
}
