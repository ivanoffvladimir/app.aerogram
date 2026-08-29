import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider, App as AntApp } from 'antd'
import ruRU from 'antd/locale/ru_RU'
import dayjs from 'dayjs'
import 'dayjs/locale/ru'

import { App } from './App'
import './index.css'

// Интерфейс, документы, письма и тексты ошибок — на русском (раздел 11 ТЗ).
dayjs.locale('ru')

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Котировка живёт 15 минут (раздел 5.3 ТЗ), остальные данные обновляются
      // по действию пользователя, а не по фокусу окна.
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 30_000,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ConfigProvider
      locale={ruRU}
      theme={{ token: { colorPrimary: '#1668dc', borderRadius: 6, fontSize: 14 } }}
    >
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  </StrictMode>,
)
