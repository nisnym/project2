import { createContext, useContext } from 'react'

/** Split from the provider so `auth.jsx` exports only a component. */
export const AuthContext = createContext(null)

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>')
  return context
}
