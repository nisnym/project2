import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { AuthProvider } from './lib/auth'
import { ToastProvider } from './components/kit'
import './styles/tokens.css'
import './styles/base.css'
import './styles/components.css'
import './styles/shell.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      refetchOnWindowFocus: true,
      // A 401 or a 422 will not become a 200 by asking again, and retrying a
      // rejected payment lookup just delays telling the customer.
      retry: (failureCount, error) =>
        failureCount < 2 && Boolean(error?.retryable) && error?.status >= 500,
    },
    mutations: { retry: false },
  },
})

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
