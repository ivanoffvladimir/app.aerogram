/**
 * Решение → строки, понятные человеку.
 *
 * Разбор живёт отдельно от экранов по той же причине, что и `routingRules.ts`:
 * «кто выбрал» — это правило, которое надо проверить тестом, а не разметка,
 * которую надо посмотреть глазами. И нужно оно двум экранам сразу — карточке
 * отправления и расчёту, — а две копии однажды разойдутся молча.
 */

import type { AutoDecision, Decision } from '@/api/client'
import { OVERRIDE_REASON_LABELS } from './overrideReason'
import { SELECTION_LABELS } from './routingRules'

/**
 * Кто принял решение.
 *
 * Три ответа, а не два: машинное решение бывает и от правила владельца,
 * и от интеграции по API. В споре с клиентом это разные разговоры, и свести
 * их к «автоматически» значит потерять тот единственный, который объясним.
 */
export function decisionMaker(decision: Decision): string {
  if (decision.mode !== 'auto') return 'Оператор'
  return decision.auto_select_rule_name
    ? `Правило «${decision.auto_select_rule_name}»`
    : 'Интеграция по API'
}

/**
 * Чем правило руководствовалось: имя правила и его признак.
 *
 * Названы оба: признак объясняет выбор, имя отвечает на вопрос «чьё это
 * правило» — за ним человек идёт на экран правил. Неизвестный признак
 * показывается как есть: словарь мог пополниться на бэкенде раньше,
 * чем здесь, и прочерк вместо значения был бы хуже машинного кода.
 */
export function autoRuleSummary(auto: AutoDecision): string {
  return `Правило «${auto.rule_name}»: ${SELECTION_LABELS[auto.rule] ?? auto.rule}`
}

/**
 * Отказ от рекомендации словами.
 *
 * «Нет» проговаривается полностью: пустая клетка на карточке читается
 * как «неизвестно», а это ровно тот случай, когда известно.
 */
export function overrideSummary(decision: Decision): string {
  if (!decision.override) return 'нет, выбран рекомендованный вариант'
  const reason = decision.override_reason
  if (!reason) return 'да'
  return OVERRIDE_REASON_LABELS[reason] ?? reason
}
