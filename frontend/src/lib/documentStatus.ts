/**
 * Печатная форма → строка, понятная кладовщику.
 *
 * Отдельно от экрана по той же причине, что `decision.ts`: «форма ещё
 * готовится» и «форма не будет готова» — разные утверждения, и путать их
 * нельзя. Кладовщик по первому ждёт, по второму идёт разбираться.
 */

import type { ShipmentDocument } from '@/api/client'

export const DOCUMENT_TYPE_LABELS: Record<string, string> = {
  label: 'Этикетка',
  waybill: 'Накладная',
  manifest: 'Манифест',
  inventory: 'Опись вложения',
  acceptance_register: 'Реестр приёма-передачи',
}

/** Подпись документа: тип и формат файла. */
export function documentTitle(document: ShipmentDocument): string {
  const type = DOCUMENT_TYPE_LABELS[document.type] ?? document.type
  return `${type} · ${document.format.toUpperCase()}`
}

/**
 * Что написать про состояние.
 *
 * У неудачи показывается причина перевозчика, а не наше «не получилось»:
 * «Форма для этого заказа недоступна» говорит кладовщику, что делать,
 * а общая формулировка отправляет его в поддержку.
 */
export function documentState(document: ShipmentDocument): string {
  if (document.status === 'ready') return 'готова'
  if (document.status === 'pending') return 'перевозчик формирует форму'
  // «Отслужила» и «не получилось» — разные вещи, и путать их нельзя:
  // по второму кладовщик пойдёт разбираться с перевозчиком, хотя груз
  // давно доставлен, а форма просто удалена вместе с персональными
  // данными получателя (ADR-0030).
  if (document.status === 'expired') return 'файл удалён: форма отслужила'
  return document.error ?? 'не удалось получить форму'
}

/** Можно ли скачать. Неготовый файл скачать нечем — его ещё нет. */
export function isDownloadable(document: ShipmentDocument): boolean {
  return document.status === 'ready'
}
