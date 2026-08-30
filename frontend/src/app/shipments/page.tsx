'use client'

import { AppShell } from '@/components/AppShell'

/**
 * Реестр отправлений. Эндпоинт `GET /v1/shipments` ещё не реализован
 * на бэкенде, поэтому экран честно говорит об этом, а не показывает
 * пустую таблицу, которую можно принять за «отправлений нет».
 */
export default function ShipmentsPage() {
  return (
    <AppShell>
      <h1>Отправления</h1>
      <div
        style={{
          background: 'var(--surface)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius)',
          padding: 24,
          color: 'var(--text-muted)',
        }}
      >
        Создание отправлений ещё не реализовано: эндпоинт <code>POST /v1/shipments</code> входит
        в оставшуюся часть контракта. Сейчас доступны расчёт, рекомендация и фиксация решения.
      </div>
    </AppShell>
  )
}
