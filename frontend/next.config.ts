import type { NextConfig } from 'next'

/**
 * Кабинет живёт за авторизацией, поэтому серверный рендеринг как продуктовая
 * возможность не используется (ADR-0012). Next взят ради контракта, генерации
 * клиента из OpenAPI и инструментов.
 *
 * Отсюда два режима сборки. В разработке фронт и API живут на разных портах,
 * и Next проксирует `/v1` на бэкенд: CORS на бэкенде намеренно выключен,
 * потому что в проде источник один. В проде собирается статика, которую
 * отдаёт Caddy — он же проксирует `/v1` на приложение.
 */
const isStaticExport = process.env.NEXT_OUTPUT === 'export'

const config: NextConfig = {
  reactStrictMode: true,
  // Линтер запускается отдельной командой (`pnpm lint`), а не внутри сборки:
  // eslint-config-next несовместим с плоской конфигурацией.
  eslint: { ignoreDuringBuilds: true },
  ...(isStaticExport
    ? { output: 'export' as const, trailingSlash: true }
    : {
        async rewrites() {
          const backend = process.env.API_ORIGIN ?? 'http://127.0.0.1:8000'
          return [{ source: '/v1/:path*', destination: `${backend}/v1/:path*` }]
        },
      }),
}

export default config
