/**
 * Причины отказа от рекомендации (раздел 5 фронт-ТЗ). Список закрытый:
 * свободный текст не сворачивается в метрику Override Rate, ради которой
 * поле и существует.
 *
 * Живёт в lib, а не в диалоге выбора: те же подписи нужны сводке кабинета,
 * а держать два списка значит однажды показать на дашборде `cheaper`.
 */
export const OVERRIDE_REASONS = [
  { value: 'cheaper', label: 'Дешевле' },
  { value: 'faster', label: 'Быстрее' },
  { value: 'recipient_requirement', label: 'Требование получателя' },
  { value: 'corporate_policy', label: 'Корпоративная политика или договор' },
  { value: 'negative_experience', label: 'Негативный опыт с рекомендованным' },
  { value: 'carrier_preference', label: 'Предпочтение перевозчика' },
  { value: 'other', label: 'Другое' },
] as const

/**
 * Причина, которую человек выбрать не может: так помечается решение правила
 * автовыбора (ADR-0029). В списке диалога её нет намеренно — оператор
 * не должен уметь сослаться на правило, которое не срабатывало, — но
 * подпись нужна карточке отправления, где такое решение показывается.
 */
const AUTO_SELECT_RULE_LABEL = 'Правило автовыбора'

/** Подпись по коду причины. Неизвестный код показывается как есть. */
export const OVERRIDE_REASON_LABELS: Record<string, string> = {
  ...Object.fromEntries(OVERRIDE_REASONS.map((reason) => [reason.value, reason.label])),
  auto_select_rule: AUTO_SELECT_RULE_LABEL,
}
