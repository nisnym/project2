/** Date, status and label formatting shared across the four shells. */

const DATE_TIME = new Intl.DateTimeFormat('en-IN', {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const DATE_ONLY = new Intl.DateTimeFormat('en-IN', {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
})

const TIME_ONLY = new Intl.DateTimeFormat('en-IN', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

export function formatDateTime(value) {
  if (!value) return '—'
  return DATE_TIME.format(new Date(value)).replace(',', '')
}

export function formatDate(value) {
  if (!value) return '—'
  return DATE_ONLY.format(new Date(value))
}

export function formatTime(value) {
  if (!value) return '—'
  return TIME_ONLY.format(new Date(value))
}

/** "4m ago", "in 2h" — and a hard "overdue" for anything past an SLA. */
export function relativeTime(value) {
  if (!value) return '—'
  const deltaMs = new Date(value).getTime() - Date.now()
  const past = deltaMs < 0
  const seconds = Math.abs(deltaMs) / 1000

  const units = [
    [60, 's'],
    [3600, 'm'],
    [86400, 'h'],
    [Infinity, 'd'],
  ]
  const divisors = [1, 60, 3600, 86400]

  let index = units.findIndex(([limit]) => seconds < limit)
  if (index === -1) index = units.length - 1
  const amount = Math.floor(seconds / divisors[index])
  const unit = units[index][1]

  if (amount === 0) return 'now'
  return past ? `${amount}${unit} ago` : `in ${amount}${unit}`
}

export function isOverdue(value) {
  return Boolean(value) && new Date(value).getTime() < Date.now()
}

/** SCREAMING_SNAKE -> Sentence case, for statuses we do not have prose for. */
export function humanise(value) {
  if (!value) return ''
  return String(value)
    .toLowerCase()
    .split('_')
    .join(' ')
    .replace(/^./, (c) => c.toUpperCase())
}

/**
 * Which visual band a transaction status belongs to.
 * Kept in one place because eight screens render a status and they must never
 * disagree about whether SETTLED is good news.
 */
const STATUS_TONE = {
  SETTLED: 'allow',
  POSTED: 'allow',
  DISPATCHED: 'allow',
  APPROVED: 'allow',
  UNDER_REVIEW: 'review',
  SCREENING: 'review',
  COMPENSATION_PENDING: 'review',
  RETURNED: 'review',
  BLOCKED: 'block',
  REJECTED: 'block',
  FAILED: 'block',
  REVERSED: 'block',
  CANCELLED: 'neutral',
  EXPIRED: 'neutral',
  INITIATED: 'pending',
  VALIDATED: 'pending',
  RESERVED: 'pending',
}

export function statusTone(status) {
  return STATUS_TONE[status] ?? 'neutral'
}

/** Statuses where money is still moving, so the UI should keep polling. */
const IN_FLIGHT = new Set([
  'INITIATED',
  'VALIDATED',
  'RESERVED',
  'SCREENING',
  'APPROVED',
  'POSTED',
  'DISPATCHED',
  'COMPENSATION_PENDING',
])

export function isInFlight(status) {
  return IN_FLIGHT.has(status)
}

export const RAIL_LABELS = {
  INTERNAL: 'Within IND Bank',
  DOMESTIC: 'Domestic (IMPS/NEFT)',
  INTERNATIONAL: 'International (SWIFT)',
  BANK_DEBIT: 'External bank debit',
  CARD: 'Debit card',
  WALLET: 'Wallet',
}

export const RAIL_NOTE = {
  INTERNAL: 'Instant. No charge.',
  DOMESTIC: 'Usually within minutes.',
  INTERNATIONAL: 'One to three working days. FX applies.',
  BANK_DEBIT: 'Clears in one working day.',
  CARD: 'Instant. Card limits apply.',
  WALLET: 'Instant.',
}

export function shortRef(value, size = 8) {
  if (!value) return '—'
  const text = String(value)
  return text.length <= size ? text : `${text.slice(0, size)}…`
}
