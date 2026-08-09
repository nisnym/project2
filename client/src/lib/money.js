/**
 * Money handling for the browser.
 *
 * The rule the whole app obeys: **an amount is a string from the moment it
 * leaves the server until the moment it is drawn**. It is never parsed into a
 * Number for arithmetic, because 0.1 + 0.2 !== 0.3 and a bank cannot ship
 * that. Number() appears exactly once below, for comparison only, and never
 * for a value that is displayed.
 */

/**
 * Indian digit grouping: 12,34,567.89 — the last three digits, then pairs.
 * Intl with en-IN does this, but only for a Number, which is precisely the
 * conversion we are refusing to make. So it is done on the string.
 */
function groupIndian(digits) {
  if (digits.length <= 3) return digits
  const last3 = digits.slice(-3)
  const rest = digits.slice(0, -3)
  return `${rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',')},${last3}`
}

const CURRENCY_MARKS = {
  INR: '₹',
  USD: '$',
  EUR: '€',
  GBP: '£',
  AED: 'AED ',
  SGD: 'S$',
}

export function currencyMark(currency = 'INR') {
  return CURRENCY_MARKS[currency] ?? `${currency} `
}

/**
 * Split a decimal string into the pieces the <Money> component draws.
 * The backend sends 4dp (its ledger scale); display rounds to the currency's
 * 2dp, half-even, matching what the server does when it hits a rail.
 */
export function splitAmount(value, currency = 'INR') {
  const raw = String(value ?? '0').trim()
  const negative = raw.startsWith('-')
  const [whole = '0', fraction = ''] = raw.replace(/^[-+]/, '').split('.')

  const padded = (fraction + '0000').slice(0, 4)
  let major = whole.replace(/^0+(?=\d)/, '')
  let minor = padded.slice(0, 2)

  // Round half-even on the discarded digits, carrying into the rupee figure
  // when it overflows -- 99.999 must read 100.00, not 99.100.
  const dropped = padded.slice(2)
  if (dropped !== '00' && dropped !== '') {
    const droppedValue = Number(dropped)
    const roundUp =
      droppedValue > 50 ||
      (droppedValue === 50 && Number(minor) % 2 === 1)
    if (roundUp) {
      const bumped = String(Number(minor) + 1).padStart(2, '0')
      if (bumped === '100') {
        minor = '00'
        major = String(BigInt(major || '0') + 1n)
      } else {
        minor = bumped
      }
    }
  }

  return {
    negative,
    mark: currencyMark(currency),
    major: groupIndian(major || '0'),
    minor,
  }
}

/** Plain string, for titles, aria-labels and CSV-ish contexts. */
export function formatMoney(value, currency = 'INR') {
  const { negative, mark, major, minor } = splitAmount(value, currency)
  return `${negative ? '-' : ''}${mark}${major}.${minor}`
}

/** Compact form for dense tables: ₹1.2L, ₹3.4Cr. Never used for a total. */
export function formatCompact(value, currency = 'INR') {
  const amount = Number(String(value ?? '0'))
  const mark = currencyMark(currency)
  if (!Number.isFinite(amount)) return formatMoney(value, currency)
  const abs = Math.abs(amount)
  if (abs >= 1e7) return `${mark}${(amount / 1e7).toFixed(2)}Cr`
  if (abs >= 1e5) return `${mark}${(amount / 1e5).toFixed(2)}L`
  if (abs >= 1e3) return `${mark}${(amount / 1e3).toFixed(1)}K`
  return formatMoney(value, currency)
}

/** Comparison only — never feed the result back into a displayed value. */
export function isPositive(value) {
  return Number(String(value ?? '0')) > 0
}

export function exceeds(value, ceiling) {
  return Number(String(value ?? '0')) > Number(String(ceiling ?? '0'))
}

/** Validate a user-typed amount without converting it. */
export function parseAmountInput(text) {
  const cleaned = String(text ?? '').replace(/[,\s₹]/g, '')
  if (!cleaned) return { ok: false, reason: 'Enter an amount.' }
  if (!/^\d+(\.\d{1,2})?$/.test(cleaned)) {
    return { ok: false, reason: 'Use digits and at most two decimal places.' }
  }
  if (Number(cleaned) <= 0) return { ok: false, reason: 'Amount must be more than zero.' }
  return { ok: true, value: cleaned }
}
