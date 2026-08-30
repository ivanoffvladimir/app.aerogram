import type { Shipment } from '@/api/client'

/**
 * Подписи статусов отправления. Словарь контракта, а не наш внутренний.
 *
 * Тип намеренно `Record<Shipment['status'], string>`: если в контракте
 * появится новый статус, сборка упадёт здесь, а не покажет оператору
 * английское слово посреди русского экрана.
 */
export const SHIPMENT_STATUS_LABELS: Record<Shipment['status'], string> = {
  Draft: 'Черновик',
  Quoted: 'Рассчитано',
  Created: 'Создано',
  PickedUp: 'Забрано',
  InTransit: 'В пути',
  OutForDelivery: 'На доставке',
  Delivered: 'Доставлено',
  Exception: 'Проблема',
  Delayed: 'Задержка',
  Cancelled: 'Отменено',
}

/** Статусы, из которых отменять уже нечего. Совпадают с проверкой бэкенда. */
export const FINAL_STATUSES = new Set(['Delivered', 'Cancelled'])

/**
 * Цветовая группа статуса. Проблему и задержку оператор обязан замечать
 * не читая, а доставленное не должно кричать наравне с ними.
 */
export function statusTone(status: string): 'ok' | 'warn' | 'muted' | 'plain' {
  if (status === 'Delivered') return 'ok'
  if (status === 'Exception' || status === 'Delayed') return 'warn'
  if (status === 'Cancelled' || status === 'Draft') return 'muted'
  return 'plain'
}

/** Нормализованные статусы ленты — они приходят из нашего словаря, не из контракта. */
export const EVENT_STATUS_LABELS: Record<string, string> = {
  DRAFT: 'Черновик',
  CREATED: 'Создано',
  ACCEPTED: 'Принято перевозчиком',
  PICKED_UP: 'Груз забран',
  AT_ORIGIN_HUB: 'На складе отправления',
  IN_TRANSIT: 'В пути',
  AT_DESTINATION_HUB: 'На складе назначения',
  OUT_FOR_DELIVERY: 'Передано на доставку',
  READY_FOR_PICKUP: 'Готово к выдаче',
  DELIVERY_ATTEMPT_FAILED: 'Неудачная попытка вручения',
  DELIVERED: 'Доставлено',
  RETURN_IN_PROGRESS: 'Оформлен возврат',
  RETURNED: 'Возвращено отправителю',
  CANCELLED: 'Отменено',
  EXCEPTION: 'Проблема',
}
