import { newIdempotencyKey } from '@/api/client'

/**
 * Ключи идемпотентности, живущие столько же, сколько намерение оператора.
 *
 * Ключ обязан переживать повтор: если создавать его на каждую попытку,
 * повтор после потерянного ответа создаст ВТОРОЕ решение по той же
 * рекомендации — ровно то, от чего идемпотентность защищает.
 */
export class IdempotencyKeys {
  private readonly keys = new Map<string, string>()

  /** Ключ для действия над объектом. Повторный вызов возвращает тот же. */
  for(subject: string): string {
    const existing = this.keys.get(subject)
    if (existing) return existing
    const key = newIdempotencyKey()
    this.keys.set(subject, key)
    return key
  }

  /** Сбросить всё: новое исходное состояние — новые намерения. */
  clear(): void {
    this.keys.clear()
  }
}
