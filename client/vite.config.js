import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The estate is ten Django services on ten ports. Rather than teach the SPA
// about any of that, the dev server fans `/api/*` out by path prefix -- so the
// browser only ever sees one origin and CORS never enters the picture.
//
// The prefixes do not collide by construction: each service owns its own
// segment under /api. Order matters only in that the longest prefix must be
// listed first, which `Object.entries` preserves.
const SERVICES = {
  '/api/auth': 8001,
  '/api/admin': 8001,
  '/.well-known': 8001,
  '/api/onboarding': 8002,
  '/api/kyc': 8003,
  '/api/accounts': 8004,
  '/api/beneficiaries': 8004,
  '/api/limit-policies': 8004,
  '/api/transactions': 8005,
  '/api/transfers': 8005,
  '/api/funding': 8005,
  '/api/funding-sources': 8005,
  '/api/schedules': 8005,
  '/api/staff/transactions': 8005,
  '/api/ledger': 8006,
  '/api/fraud': 8007,
  '/api/notifications': 8008,
  '/api/audit': 8009,
  '/api/ops': 8010,
}

const proxy = Object.fromEntries(
  Object.entries(SERVICES).map(([prefix, port]) => [
    prefix,
    {
      target: `http://127.0.0.1:${port}`,
      changeOrigin: true,
      // A service that is simply not running should read as "unavailable" in
      // the UI, not as an opaque proxy stack trace in the terminal.
      configure: (proxyServer) => {
        proxyServer.on('error', (err, _req, res) => {
          if (res && !res.headersSent && res.writeHead) {
            res.writeHead(503, { 'Content-Type': 'application/json' })
            res.end(
              JSON.stringify({
                error: {
                  code: 'SERVICE_UNAVAILABLE',
                  message: `${prefix} -> :${port} is not running (${err.code}).`,
                  retryable: true,
                },
              }),
            )
          }
        })
      },
    },
  ]),
)

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
})
