'use client'

import { useRouter } from 'next/navigation'
import { useEffect } from 'react'
import { tokens } from '@/api/client'

/**
 * Переход на клиенте, а не серверный redirect: страница собирается в статику,
 * и решение зависит от токена, которого на сервере нет.
 */
export default function IndexPage() {
  const router = useRouter()

  useEffect(() => {
    router.replace(tokens.access() ? '/rate-shopping' : '/login')
  }, [router])

  return null
}
