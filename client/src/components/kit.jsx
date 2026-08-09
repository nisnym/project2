/**
 * The boxy component kit.
 *
 * Every screen in the app is assembled from these. Keeping them in one file
 * is deliberate: it is small enough to read end to end, which is what stops a
 * ninth variant of "a box with a heading" from being invented on screen ten.
 */

/* eslint-disable react-refresh/only-export-components --
   The kit intentionally exports its hooks (useToast, useIdempotencyKey)
   alongside its components; splitting three-line hooks into their own modules
   would cost more in indirection than fast refresh is worth here. */
import { createContext, useCallback, useContext, useId, useMemo, useState } from 'react'
import { formatMoney, splitAmount } from '../lib/money'
import { humanise, statusTone } from '../lib/format'

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export function Panel({
  title,
  action,
  children,
  flush = false,
  tone = 'default',
  ticks = false,
  footer,
  className = '',
}) {
  const toneClass =
    tone === 'raised' ? 'panel--raised' : tone === 'framed' ? 'panel--framed' : ''
  return (
    <section className={`panel ${toneClass} ${ticks ? 'panel__ticks' : ''} ${className}`}>
      {(title || action) && (
        <header className="panel__head">
          <h2 className="panel__title">{title}</h2>
          {action}
        </header>
      )}
      <div className={flush ? 'panel__body--flush' : 'panel__body'}>{children}</div>
      {footer && <footer className="panel__foot">{footer}</footer>}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Money
// ---------------------------------------------------------------------------

export function Money({ value, currency = 'INR', size, tone, signed = false, className = '' }) {
  const { negative, mark, major, minor } = splitAmount(value, currency)
  const sizeClass = size === 'display' ? 'money--display' : size === 'lg' ? 'money--lg' : ''
  const toneClass =
    tone === 'credit' ? 'money--credit' : tone === 'debit' ? 'money--debit' : ''
  const sign = signed ? (tone === 'debit' ? '−' : tone === 'credit' ? '+' : '') : negative ? '−' : ''

  return (
    <span
      className={`money ${sizeClass} ${toneClass} ${className}`}
      title={formatMoney(value, currency)}
    >
      {sign}
      <span className="money__mark">{mark}</span>
      {major}
      <span className="money__minor">.{minor}</span>
    </span>
  )
}

// ---------------------------------------------------------------------------
// Stamp — status and fraud decisions
// ---------------------------------------------------------------------------

export function Stamp({ tone = 'neutral', working = false, children }) {
  return (
    <span className={`stamp stamp--${tone} ${working ? 'stamp--working' : ''}`}>
      {children}
    </span>
  )
}

export function StatusStamp({ status, working }) {
  return (
    <Stamp tone={statusTone(status)} working={working}>
      {humanise(status)}
    </Stamp>
  )
}

export function DecisionStamp({ decision }) {
  const tone = decision === 'ALLOW' ? 'allow' : decision === 'BLOCK' ? 'block' : 'review'
  return <Stamp tone={tone}>{decision}</Stamp>
}

// ---------------------------------------------------------------------------
// Score meter
// ---------------------------------------------------------------------------

export function ScoreMeter({ score = 0, allowBelow = 40, blockAt = 75, cells = 20 }) {
  const filled = Math.round((Math.max(0, Math.min(100, score)) / 100) * cells)
  const band = score >= blockAt ? 'block' : score >= allowBelow ? 'review' : 'allow'

  return (
    <div>
      <div
        className="meter"
        role="meter"
        aria-valuenow={score}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`Fraud score ${score} of 100 — ${band} band`}
      >
        {Array.from({ length: cells }, (_, index) => (
          <div
            key={index}
            className="meter__cell"
            data-on={index < filled}
            data-band={band}
          />
        ))}
      </div>
      <div className="meter__scale">
        <span className="label">0 · allow &lt;{allowBelow}</span>
        <span className="label">review</span>
        <span className="label">block ≥{blockAt} · 100</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Form fields
// ---------------------------------------------------------------------------

export function Field({ label, hint, error, children, id }) {
  const generatedId = useId()
  const fieldId = id ?? generatedId
  return (
    <div className="field">
      <div className="field__label">
        <label className="label label--ink" htmlFor={fieldId}>
          {label}
        </label>
        {hint && <span className="field__hint">{hint}</span>}
      </div>
      {typeof children === 'function' ? children(fieldId, Boolean(error)) : children}
      {error && (
        <p className="field__error" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}

export function TextInput({ mono = false, invalid = false, className = '', ...props }) {
  return (
    <input
      className={`input ${mono ? 'input--mono' : ''} ${className}`}
      aria-invalid={invalid || undefined}
      {...props}
    />
  )
}

export function AmountInput({ currency = 'INR', invalid = false, ...props }) {
  return (
    <div className="amount-wrap">
      <span className="amount-wrap__mark">{currency === 'INR' ? '₹' : currency}</span>
      <input
        className="input input--amount"
        inputMode="decimal"
        autoComplete="off"
        aria-invalid={invalid || undefined}
        {...props}
      />
    </div>
  )
}

export function Select({ options, className = '', ...props }) {
  return (
    <select className={`select ${className}`} {...props}>
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  )
}

// ---------------------------------------------------------------------------
// Buttons
// ---------------------------------------------------------------------------

export function Button({
  variant = 'default',
  size,
  block = false,
  busy = false,
  children,
  className = '',
  ...props
}) {
  const variantClass = variant === 'default' ? '' : `btn--${variant}`
  return (
    <button
      className={`btn ${variantClass} ${size === 'sm' ? 'btn--sm' : ''} ${
        block ? 'btn--block' : ''
      } ${className}`}
      disabled={busy || props.disabled}
      {...props}
    >
      {busy ? 'Working…' : children}
    </button>
  )
}

// ---------------------------------------------------------------------------
// Data display
// ---------------------------------------------------------------------------

export function KeyValue({ rows }) {
  return (
    <dl className="kv">
      {rows
        .filter(([, value]) => value !== undefined && value !== null && value !== '')
        .map(([key, value]) => (
          <Fragment2 key={key}>
            <dt>{key}</dt>
            <dd>{value}</dd>
          </Fragment2>
        ))}
    </dl>
  )
}

// A named fragment so the <dl> gets dt/dd as direct children — the grid
// depends on it, and a wrapping <div> would silently break the columns.
function Fragment2({ children }) {
  return <>{children}</>
}

export function Stat({ label, value, tone = 'default', note }) {
  return (
    <div className={`stat ${tone === 'default' ? '' : `stat--${tone}`}`}>
      <div className="label">{label}</div>
      <div className="stat__value">{value}</div>
      {note && <div className="tiny faint" style={{ marginTop: 4 }}>{note}</div>}
    </div>
  )
}

export function Empty({ mark = '— · —', title, children, action }) {
  return (
    <div className="empty">
      <div className="empty__mark">{mark}</div>
      {title && <p style={{ fontWeight: 600, color: 'var(--ink)' }}>{title}</p>}
      {children && <p className="small" style={{ marginTop: 8 }}>{children}</p>}
      {action && <div style={{ marginTop: 20 }}>{action}</div>}
    </div>
  )
}

export function Loading({ rows = 4 }) {
  return (
    <div className="stack" style={{ '--gap': '10px', padding: 'var(--s-4)' }}>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="skeleton" style={{ width: `${100 - index * 11}%` }} />
      ))}
    </div>
  )
}

export function Banner({ tone = 'info', title, children }) {
  return (
    <div className={`banner banner--${tone}`} role={tone === 'error' ? 'alert' : undefined}>
      {title && <div className="banner__title">{title}</div>}
      <div>{children}</div>
    </div>
  )
}

/**
 * Renders whatever the API threw.
 *
 * Shows the correlation id when there is one: it is the string that lets
 * support pull the whole cross-service trace out of the audit log, and it is
 * useless if the UI swallows it.
 */
export function ErrorBanner({ error, title = 'That did not go through' }) {
  if (!error) return null
  return (
    <Banner tone="error" title={title}>
      <div>{error.message}</div>
      {error.code && (
        <div className="tiny mono" style={{ marginTop: 6, opacity: 0.75 }}>
          {error.code}
          {error.correlationId ? ` · trace ${error.correlationId}` : ''}
        </div>
      )}
    </Banner>
  )
}

export function Tabs({ tabs, active, onChange }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.value}
          role="tab"
          className="tab"
          aria-selected={active === tab.value}
          onClick={() => onChange(tab.value)}
        >
          {tab.label}
          {tab.count !== undefined && ` (${tab.count})`}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------------

const ToastContext = createContext(null)

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const push = useCallback((message, tone = 'info') => {
    const id = crypto.randomUUID()
    setToasts((current) => [...current, { id, message, tone }])
    setTimeout(() => {
      setToasts((current) => current.filter((toast) => toast.id !== id))
    }, 5200)
  }, [])

  const value = useMemo(
    () => ({
      push,
      ok: (message) => push(message, 'ok'),
      error: (message) => push(message, 'error'),
    }),
    [push],
  )

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast toast--${toast.tone}`}>
            {toast.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  return useContext(ToastContext) ?? { push: () => {}, ok: () => {}, error: () => {} }
}

// ---------------------------------------------------------------------------
// Idempotency key
// ---------------------------------------------------------------------------

/**
 * One key per opened form, not per submit.
 *
 * This is the detail that makes a double-submit safe. If the key were minted
 * on click, an impatient second click would carry a *different* key and the
 * server would treat it as a second, genuine payment. Minting it when the form
 * opens means every retry of that one intent shares a key, and the backend
 * replays the original response instead of moving money twice.
 *
 * `reset` is called after a success so the next payment is a new intent.
 */
export function useIdempotencyKey(dependency = '') {
  // Derived, not synchronised in an effect: the key is a pure function of the
  // form's identity and how many times it has been reset, so there is no window
  // where a render has a stale key.
  const [generation, setGeneration] = useState(0)
  // The deps look unused to the linter because the factory ignores them — that
  // is the intent. They are the cache key: a new form identity or an explicit
  // reset mints a new UUID, and nothing else does.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const key = useMemo(() => crypto.randomUUID(), [dependency, generation])
  return [key, useCallback(() => setGeneration((n) => n + 1), [])]
}
