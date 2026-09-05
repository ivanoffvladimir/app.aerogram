/**
 * Тело PATCH: только то, что оператор действительно изменил.
 *
 * Сервер отличает «поле не передано» от «передано пустым»: непереданное
 * остаётся как было, а `null` очищает. Поэтому отправлять форму целиком
 * нельзя — она затёрла бы чужую правку, сделанную минуту назад в соседней
 * вкладке, значениями, которых оператор не касался.
 */

/** Пустая строка в форме означает «очистить», а не «оставить пустым». */
function normalize(value: string | boolean | null): string | boolean | null {
  if (typeof value !== 'string') return value
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

/**
 * Изменившиеся поля черновика по сравнению с исходником.
 *
 * Пустой объект означает «править нечего» — вызывающий вправе не слать
 * запрос вовсе.
 */
export function changedFields<T extends Record<string, string | boolean | null>>(
  original: T,
  draft: T,
): Partial<T> {
  const patch: Record<string, string | boolean | null> = {}
  for (const key of Object.keys(draft)) {
    const next = normalize(draft[key] ?? null)
    const prev = normalize(original[key] ?? null)
    if (next !== prev) patch[key] = next
  }
  return patch as Partial<T>
}
