/**
 * Подписи и правила экрана сверки расчёта и счетов.
 *
 * Вынесено из страницы: страницу нельзя вызвать из теста, а ошибка в знаке
 * разницы или в подписи состояния — это ошибка про деньги клиента.
 */

import type { CurrencyTotals, Money, ReconciliationState } from '@/api/client'

/** Русские подписи состояний. Ключи типизированы клиентом: новое состояние
 *  на сервере ломает сборку, а не показывается человеку по-английски. */
export const STATE_LABELS: Record<ReconciliationState, string> = {
  awaiting: 'Ждёт счёта',
  no_quote: 'Нет котировки',
  matched: 'Сошлось',
  overcharged: 'Счёт больше',
  undercharged: 'Счёт меньше',
}

/**
 * Порядок фильтра. «Все» отдельным значением, а не пустой строкой в разметке:
 * пустое значение в двух местах разошлось бы с пустым в третьем.
 */
export const STATE_ORDER: ReconciliationState[] = [
  'overcharged',
  'undercharged',
  'matched',
  'awaiting',
  'no_quote',
]

/**
 * Знак разницы для окраски строки.
 *
 * `over` — перевозчик выставил больше обещанного, это плохая новость;
 * `under` — меньше; `zero` — сошлось или сравнивать нечего.
 *
 * Отдельная функция, потому что ноль и «нечего сравнивать» обязаны
 * выглядеть одинаково нейтрально: подкрасив пустоту зелёным, экран сказал бы
 * «сэкономили», хотя счёт просто не пришёл.
 */
export function differenceTone(difference: Money | null): 'over' | 'under' | 'zero' {
  if (!difference || difference.amount_minor === 0) return 'zero'
  return difference.amount_minor > 0 ? 'over' : 'under'
}

/**
 * Сколько отправлений периода вообще участвовало в сверке.
 *
 * Знаменатель — это НЕ все отправления: те, по которым счёт не приходил,
 * сверять не с чем. Число нужно рядом с итогом, иначе «расхождений нет»
 * читается как «перевозчик выставляет ровно то, что обещал».
 */
export function reconciledCount(totals: CurrencyTotals): number {
  return totals.matched + totals.overcharged + totals.undercharged
}

/**
 * Есть ли вообще что сверять. Экран без единого счёта обязан сказать это
 * словами, а не показывать нули: нули читаются как «всё сошлось».
 */
export function hasAnythingToReconcile(currencies: CurrencyTotals[]): boolean {
  return currencies.some((totals) => reconciledCount(totals) > 0)
}
