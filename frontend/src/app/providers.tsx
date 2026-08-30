'use client'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import { ApiError } from '@/api/client'

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Расчёт живёт минуты: показывать его из кэша дольше значит
            // предлагать оператору цену, которой уже нет.
            staleTime: 30_000,
            retry: (failureCount, error) => {
              // Ошибку прав или валидации повтор не исправит, а 401 должен
              // приводить ко входу, а не к трём одинаковым отказам.
              if (error instanceof ApiError && error.status < 500) return false
              return failureCount < 2
            },
          },
          mutations: { retry: false },
        },
      }),
  )

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}
