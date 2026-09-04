/**
 * Подписи состояний массового расчёта.
 *
 * Русские: их читает оператор. Ключи типизированы контрактом клиента,
 * поэтому новое состояние на сервере ломает сборку, а не показывается
 * человеку по-английски.
 */

import type { BulkRowStatus, BulkRunStatus } from '@/api/client'

export const RUN_STATUS_LABELS: Record<BulkRunStatus, string> = {
  draft: 'Черновик',
  quoting: 'Считается',
  quoted: 'Посчитан',
  creating: 'Оформляется',
  completed: 'Завершён',
  failed: 'Не прошёл',
}

export const ROW_STATUS_LABELS: Record<BulkRowStatus, string> = {
  new: 'Не посчитана',
  quoted: 'Посчитана',
  selected: 'Выбран тариф',
  created: 'Оформлена',
  failed: 'Не прошла',
}

/**
 * Следующий шаг прогона: подпись кнопки и путь.
 *
 * Шаг выводится из счётчиков строк, а не из состояния прогона. Состояние
 * прогона для этого недостаточно: после расчёта и после выбора тарифа оно
 * одинаково `quoted` — прогон стоит в работе в обоих случаях, — и кнопка
 * по нему предлагала бы считать заново то, что уже посчитано.
 *
 * Условия здесь те же, по которым сервер пропускает строку: `quote` берёт
 * строки `new`, `select` — `quoted`, `create` — `selected`. Порядок важен:
 * прогон бывает частичным, и пока есть непосчитанные строки, считать их
 * нужнее, чем оформлять уже выбранные.
 *
 * `null` — делать нечего: все строки либо оформлены, либо не прошли.
 * Показывать кнопку, которая ничего не изменит, значит предлагать оператору
 * бессмысленное действие и заставлять его гадать, почему ничего не произошло.
 */
export function nextStep(
  counts: Record<string, number>,
): { label: string; path: 'quote' | 'select' | 'create' } | null {
  if (counts.new) return { label: 'Посчитать', path: 'quote' }
  if (counts.quoted) return { label: 'Выбрать тариф', path: 'select' }
  if (counts.selected) return { label: 'Оформить', path: 'create' }
  return null
}

/** Сколько строк не прошло. Ноль — прогон чистый. */
export function failedCount(counts: Record<string, number>): number {
  return counts.failed ?? 0
}
