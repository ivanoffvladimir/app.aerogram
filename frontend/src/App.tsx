import { useEffect } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { Spin } from 'antd'

import { AppLayout } from '@/components/AppLayout'
import { LoginPage } from '@/pages/LoginPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { ShipmentsPage } from '@/pages/ShipmentsPage'
import { useAuthStore } from '@/stores/auth'

/** Маршрут, доступный только авторизованному пользователю. */
function Protected({ children }: { children: React.ReactNode }) {
  const { accessToken, user, loadProfile, logout } = useAuthStore()

  useEffect(() => {
    if (accessToken && !user) {
      // Токен мог протухнуть, пока вкладка была закрыта.
      loadProfile().catch(() => logout())
    }
  }, [accessToken, user, loadProfile, logout])

  if (!accessToken) return <Navigate to="/login" replace />
  if (!user) return <Spin fullscreen tip="Загрузка…" />
  return <>{children}</>
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <Protected>
            <AppLayout />
          </Protected>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="shipments" element={<ShipmentsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
