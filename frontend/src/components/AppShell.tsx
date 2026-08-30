'use client'

import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import type { ReactNode } from 'react'
import { tokens } from '@/api/client'
import styles from './AppShell.module.css'

/** Пункты меню P0 и P1 по разделу 2 фронт-ТЗ. */
const AVAILABLE = [
  { href: '/rate-shopping', label: 'Расчёт и выбор' },
  { href: '/shipments', label: 'Отправления' },
]

/** Экраны, эндпоинтов для которых ещё нет. Показываются приглушёнными,
 *  а не прячутся: оператор должен видеть границу готовности продукта. */
const PLANNED = ['Дашборд', 'Трекинг', 'Перевозчики', 'Carrier Score', 'Правила', 'Пользователи']

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname()
  const router = useRouter()

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <div className={styles.brand}>
          Aerogram
          <span className={styles.brandSub}>Logistics OS</span>
        </div>
        <nav className={styles.nav}>
          {AVAILABLE.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`${styles.link} ${pathname.startsWith(item.href) ? styles.active : ''}`}
            >
              {item.label}
            </Link>
          ))}
          {PLANNED.map((label) => (
            <span key={label} className={styles.disabled} title="Ещё не реализовано">
              {label}
            </span>
          ))}
        </nav>
      </aside>
      <div>
        <div className={styles.topbar}>
          <button
            type="button"
            className={styles.logout}
            onClick={() => {
              tokens.clear()
              router.replace('/login')
            }}
          >
            Выйти
          </button>
        </div>
        <main className={styles.main}>{children}</main>
      </div>
    </div>
  )
}
