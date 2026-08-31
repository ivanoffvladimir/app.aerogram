import type { ExceptionReason } from '@/api/client'

/**
 * Причины разбора в том же порядке, что и на сервере: сорванный срок первым,
 * потому что он единственный уже стоил денег, молчание перевозчика последним.
 *
 * Тип ключа — `ExceptionReason`, а не `string`: новая причина на сервере
 * обязана сломать сборку здесь, а не показаться оператору строкой
 * `deadline_passed` посреди русского экрана.
 */
export const EXCEPTION_REASONS: {
  key: ExceptionReason
  label: string
  hint: string
  tone: 'critical' | 'warn' | 'muted'
}[] = [
  {
    key: 'deadline_passed',
    label: 'Срок сорван',
    hint: 'Срок из расчёта прошёл, доставки нет',
    tone: 'critical',
  },
  {
    key: 'problem_status',
    label: 'Проблема у перевозчика',
    hint: 'Неудачное вручение, возврат или исключение',
    tone: 'warn',
  },
  {
    key: 'stalled',
    label: 'Перевозчик молчит',
    hint: 'Событий нет дольше порога опроса',
    tone: 'muted',
  },
]

const BY_KEY = new Map(EXCEPTION_REASONS.map((reason) => [reason.key, reason]))

/**
 * Описание причины. Незнакомая причина не прячется и не роняет экран:
 * оператор увидит её код и сможет назвать его поддержке — это лучше,
 * чем строка, исчезнувшая из таблицы без следа.
 */
export function describeReason(reason: string): {
  label: string
  hint: string
  tone: 'critical' | 'warn' | 'muted'
} {
  return BY_KEY.get(reason as ExceptionReason) ?? { label: reason, hint: reason, tone: 'muted' }
}
