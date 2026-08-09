import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  api,
  authenticate,
  clearTokens,
  onAuthLostSet,
  restoreSession,
  setTokens,
} from './api'
import { AuthContext } from './authContext'
import { STAFF_ROLES } from './roles'

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [ready, setReady] = useState(false)

  const signOut = useCallback(async () => {
    // Best effort: the local session is dropped whether or not the server is
    // reachable. A sign-out that fails because a service is down would be a
    // worse outcome than one that leaves a refresh token to expire.
    try {
      await api.post('/api/auth/logout', {})
    } catch {
      /* ignore */
    }
    clearTokens()
    setUser(null)
  }, [])

  useEffect(() => {
    onAuthLostSet(() => setUser(null))
  }, [])

  // On load, try to turn a surviving refresh token back into a session.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const restored = await restoreSession()
      if (cancelled) return
      if (restored) {
        try {
          const profile = await api.get('/api/auth/me')
          if (!cancelled) setUser(profile)
        } catch {
          clearTokens()
        }
      }
      if (!cancelled) setReady(true)
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const signIn = useCallback(async (email, password) => {
    const tokens = await authenticate('/api/auth/login', { email, password })
    setTokens(tokens)
    const profile = await api.get('/api/auth/me')
    setUser(profile)
    return profile
  }, [])

  const register = useCallback(
    async (payload) => {
      await authenticate('/api/auth/register', payload)
      return signIn(payload.email, payload.password)
    },
    [signIn],
  )

  const value = useMemo(
    () => ({
      user,
      ready,
      signIn,
      register,
      signOut,
      role: user?.role ?? null,
      isStaff: STAFF_ROLES.includes(user?.role),
    }),
    [user, ready, signIn, register, signOut],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
