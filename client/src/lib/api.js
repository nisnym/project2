/**
 * The single door to the backend.
 *
 * Three things live here because they must not be re-decided per call site:
 *
 *  1. The access token is held in a module variable, never in localStorage.
 *     Anything in localStorage is readable by any script that lands on the
 *     page, and a bearer token for a bank is the one thing worth stealing.
 *     The cost is that a refresh loses the session, which is why the refresh
 *     token exists.
 *
 *  2. A 401 triggers exactly one refresh, and every request that raced into
 *     the same 401 waits on that one promise. Without the shared promise, a
 *     dashboard firing six parallel queries would send six refreshes -- and
 *     since the backend rotates refresh tokens and treats reuse as theft, five
 *     of them would look like an attack and revoke the whole family.
 *
 *  3. Errors arrive as the platform's envelope, so the UI branches on a stable
 *     `code` instead of parsing prose.
 */

let accessToken = null
let refreshToken = null
let onAuthLost = () => {}

export function setTokens(next) {
  accessToken = next?.access_token ?? null
  refreshToken = next?.refresh_token ?? null
  // The refresh token is the weaker secret and needs to outlive a reload for
  // "stay signed in" to mean anything. sessionStorage keeps it out of other
  // tabs and clears with the tab.
  if (refreshToken) sessionStorage.setItem('ind.rt', refreshToken)
  else sessionStorage.removeItem('ind.rt')
}

export function storedRefreshToken() {
  return sessionStorage.getItem('ind.rt')
}

export function clearTokens() {
  accessToken = null
  refreshToken = null
  sessionStorage.removeItem('ind.rt')
}

export function onAuthLostSet(handler) {
  onAuthLost = handler
}

export function hasSession() {
  return Boolean(accessToken)
}

/** A stable per-browser device mark. The fraud rules score on device novelty. */
export function deviceFingerprint() {
  let mark = localStorage.getItem('ind.device')
  if (!mark) {
    mark = `web:${crypto.randomUUID().slice(0, 18)}`
    localStorage.setItem('ind.device', mark)
  }
  return mark
}

export class ApiError extends Error {
  constructor(status, envelope) {
    const error = envelope?.error ?? {}
    super(error.message || error.detail?.message || `Request failed (${status})`)
    this.name = 'ApiError'
    this.status = status
    this.code = error.code || `HTTP_${status}`
    this.detail = error.detail ?? {}
    this.retryable = Boolean(error.retryable)
    this.correlationId = envelope?.correlation_id ?? null
  }
}

// ---------------------------------------------------------------------------
// refresh, coalesced
// ---------------------------------------------------------------------------

let inFlightRefresh = null

async function refreshSession() {
  const token = refreshToken ?? storedRefreshToken()
  if (!token) throw new ApiError(401, { error: { code: 'NO_SESSION' } })

  const response = await fetch('/api/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: token }),
  })

  if (!response.ok) {
    clearTokens()
    onAuthLost()
    throw new ApiError(401, await safeJson(response))
  }
  const tokens = await response.json()
  setTokens(tokens)
  return tokens.access_token
}

function refreshOnce() {
  // The whole point: concurrent 401s share one refresh.
  if (!inFlightRefresh) {
    inFlightRefresh = refreshSession().finally(() => {
      inFlightRefresh = null
    })
  }
  return inFlightRefresh
}

async function safeJson(response) {
  try {
    return await response.json()
  } catch {
    return { error: { code: `HTTP_${response.status}`, message: response.statusText } }
  }
}

// ---------------------------------------------------------------------------
// request
// ---------------------------------------------------------------------------

async function send(path, { method = 'GET', body, idempotencyKey, signal } = {}) {
  const headers = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
  if (method !== 'GET') {
    headers['X-Device-Fingerprint'] = deviceFingerprint()
    headers['X-Ip-Country'] = 'IN'
  }

  return fetch(path, {
    method,
    headers,
    signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export async function request(path, options = {}) {
  let response = await send(path, options)

  if (response.status === 401 && (refreshToken || storedRefreshToken())) {
    try {
      await refreshOnce()
      response = await send(path, options)
    } catch {
      throw new ApiError(401, { error: { code: 'SESSION_EXPIRED', message: 'Please sign in again.' } })
    }
  }

  if (response.status === 204) return null
  const payload = await safeJson(response)
  if (!response.ok) throw new ApiError(response.status, payload)
  return payload
}

export const api = {
  get: (path, options) => request(path, options),
  post: (path, body, options) => request(path, { ...options, method: 'POST', body }),
  patch: (path, body, options) => request(path, { ...options, method: 'PATCH', body }),
  del: (path, options) => request(path, { ...options, method: 'DELETE' }),
}

/**
 * Sign in without a token, and seed the module state on success.
 * Kept separate from `api.post` so nothing else can accidentally send
 * credentials.
 */
export async function authenticate(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Device-Fingerprint': deviceFingerprint(),
      'X-Ip-Country': 'IN',
    },
    body: JSON.stringify(body),
  })
  const payload = await safeJson(response)
  if (!response.ok) throw new ApiError(response.status, payload)
  return payload
}

export async function restoreSession() {
  if (!storedRefreshToken()) return false
  try {
    await refreshOnce()
    return true
  } catch {
    return false
  }
}
