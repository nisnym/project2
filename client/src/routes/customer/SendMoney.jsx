import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  AmountInput,
  Banner,
  Button,
  Empty,
  ErrorBanner,
  Field,
  KeyValue,
  Money,
  Panel,
  Select,
  Stamp,
  TextInput,
  useIdempotencyKey,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { RAIL_LABELS, RAIL_NOTE } from '../../lib/format'
import { exceeds, formatMoney, parseAmountInput } from '../../lib/money'

/**
 * The transfer form.
 *
 * Two decisions worth stating:
 *
 * - The idempotency key is minted when the form *opens*, not when Send is
 *   pressed. A second click, a flaky connection, a retry — all carry the same
 *   key, so the server replays the first answer instead of sending the money
 *   twice. It is reset only after a completed transfer.
 *
 * - The outcome screen treats BLOCKED and UNDER_REVIEW as first-class results
 *   rather than errors. They are what the fraud engine is *for*, and a customer
 *   whose transfer is held needs to be told plainly what happened to the money.
 */

function railFor(beneficiary) {
  if (!beneficiary) return 'INTERNAL'
  return beneficiary.beneficiary_type === 'INTERNATIONAL'
    ? 'INTERNATIONAL'
    : beneficiary.beneficiary_type === 'INTERNAL'
      ? 'INTERNAL'
      : 'DOMESTIC'
}

function Outcome({ txn, onAgain }) {
  const navigate = useNavigate()

  const view = {
    BLOCKED: {
      tone: 'error',
      title: 'Blocked by fraud screening',
      body: 'We stopped this transfer. Nothing has left your account — the funds we were holding have been released.',
    },
    UNDER_REVIEW: {
      tone: 'warn',
      title: 'Held for review',
      body: 'One of our analysts is checking this. The money is held, not sent. You will hear from us shortly, and you can cancel in the meantime.',
    },
    REJECTED: {
      tone: 'error',
      title: 'Rejected',
      body: 'This transfer was not accepted.',
    },
  }[txn.status] ?? {
    tone: 'ok',
    title: 'On its way',
    body: 'The instruction has been accepted and sent to the payment rail.',
  }

  return (
    <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
      <Banner tone={view.tone} title={view.title}>
        {view.body}
      </Banner>

      <Panel tone="raised" ticks title="Receipt">
        <KeyValue
          rows={[
            ['Reference', <span key="r" className="mono">{txn.reference}</span>],
            ['Amount', <Money key="a" value={txn.amount} currency={txn.currency} size="lg" />],
            ['To', txn.beneficiary_masked || '—'],
            ['Route', RAIL_LABELS[txn.rail] ?? txn.rail],
            ['Status', <Stamp key="s" tone={view.tone === 'ok' ? 'allow' : view.tone === 'warn' ? 'review' : 'block'}>{txn.status}</Stamp>],
            txn.fraud_score !== null && txn.fraud_score !== undefined
              ? ['Fraud score', <span key="f" className="mono">{txn.fraud_score} / 100</span>]
              : null,
            txn.status_reason ? ['Reason', txn.status_reason] : null,
          ].filter(Boolean)}
        />
      </Panel>

      <div className="row">
        <Button variant="primary" onClick={() => navigate(`/activity/${txn.id}`)}>
          Track this transfer
        </Button>
        <Button onClick={onAgain}>Send another</Button>
      </div>
    </div>
  )
}

export default function SendMoney() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()

  const [accountId, setAccountId] = useState('')
  const [beneficiaryId, setBeneficiaryId] = useState('')
  const [amount, setAmount] = useState('')
  const [remarks, setRemarks] = useState('')
  const [result, setResult] = useState(null)

  const [idempotencyKey, resetKey] = useIdempotencyKey()

  const accounts = useQuery({ queryKey: ['accounts'], queryFn: () => api.get('/api/accounts') })
  const payees = useQuery({
    queryKey: ['beneficiaries'],
    queryFn: () => api.get('/api/beneficiaries?status=ACTIVE'),
  })

  const accountList = accounts.data?.results ?? []
  const payeeList = payees.data?.results ?? []

  const account = accountList.find((a) => a.id === accountId) ?? accountList[0]
  const beneficiary = payeeList.find((b) => b.id === beneficiaryId)
  const rail = railFor(beneficiary)

  const parsed = useMemo(() => (amount ? parseAmountInput(amount) : null), [amount])
  const overBalance =
    parsed?.ok && account && exceeds(parsed.value, account.balance.available)

  const send = useMutation({
    mutationFn: (body) => api.post('/api/transfers', body, { idempotencyKey }),
    onSuccess: (txn) => {
      setResult(txn)
      resetKey()
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['transactions'] })
      if (txn.status === 'BLOCKED') toast.error(`${txn.reference} was blocked`)
      else if (txn.status === 'UNDER_REVIEW') toast.push(`${txn.reference} is under review`, 'info')
      else toast.ok(`${txn.reference} sent`)
    },
  })

  function submit(event) {
    event.preventDefault()
    if (!parsed?.ok || !account || !beneficiary) return
    send.mutate({
      account_id: account.id,
      beneficiary_id: beneficiary.id,
      amount: parsed.value,
      currency: account.currency,
      rail,
      remarks,
    })
  }

  function again() {
    setResult(null)
    setAmount('')
    setRemarks('')
    send.reset()
  }

  if (result) {
    return (
      <Page title="Transfer" subtitle="A record of what just happened.">
        <div style={{ maxWidth: 640 }}>
          <Outcome txn={result} onAgain={again} />
        </div>
      </Page>
    )
  }

  if (payeeList.length === 0 && !payees.isLoading) {
    return (
      <Page title="Send money">
        <Panel tone="raised" ticks>
          <Empty
            mark="[ → ]"
            title="No payees yet"
            action={
              <Button variant="primary" onClick={() => navigate('/payees')}>
                Add a payee
              </Button>
            }
          >
            You need someone to pay first. New payees have a short cooling-off
            period before large transfers are allowed.
          </Empty>
        </Panel>
      </Page>
    )
  }

  return (
    <Page title="Send money" subtitle="Every transfer is screened before it leaves the bank.">
      <div className="split">
        <Panel tone="raised" ticks className="reveal">
          <form onSubmit={submit} className="stack">
            <Field label="From">
              {(id) => (
                <Select
                  id={id}
                  value={account?.id ?? ''}
                  onChange={(event) => setAccountId(event.target.value)}
                  options={accountList.map((item) => ({
                    value: item.id,
                    label: `${item.account_number} — ${formatMoney(item.balance.available, item.currency)}`,
                  }))}
                />
              )}
            </Field>

            <Field label="To">
              {(id) => (
                <Select
                  id={id}
                  value={beneficiaryId}
                  onChange={(event) => setBeneficiaryId(event.target.value)}
                  options={[
                    { value: '', label: 'Choose a payee…' },
                    ...payeeList.map((item) => ({
                      value: item.id,
                      label: `${item.nickname} — ${item.masked}`,
                    })),
                  ]}
                />
              )}
            </Field>

            {beneficiary?.in_cooling_off && (
              <Banner tone="warn" title="Recently added payee">
                This payee is still in its cooling-off period. Large transfers may
                be held for review — that is deliberate, and it is what stops an
                attacker who has taken over an account from emptying it.
              </Banner>
            )}

            <Field
              label="Amount"
              hint={account ? `Available ${formatMoney(account.balance.available, account.currency)}` : ''}
              error={
                parsed && !parsed.ok
                  ? parsed.reason
                  : overBalance
                    ? 'More than the available balance.'
                    : null
              }
            >
              {(id) => (
                <AmountInput
                  id={id}
                  currency={account?.currency ?? 'INR'}
                  value={amount}
                  placeholder="0.00"
                  invalid={Boolean((parsed && !parsed.ok) || overBalance)}
                  onChange={(event) => setAmount(event.target.value)}
                />
              )}
            </Field>

            <Field label="Reference" hint="Optional — shown to the payee">
              {(id) => (
                <TextInput
                  id={id}
                  value={remarks}
                  maxLength={140}
                  placeholder="Rent, September"
                  onChange={(event) => setRemarks(event.target.value)}
                />
              )}
            </Field>

            <ErrorBanner error={send.error} title="Transfer not accepted" />

            <Button
              type="submit"
              variant="primary"
              busy={send.isPending}
              disabled={!parsed?.ok || !beneficiary || overBalance}
            >
              Send {parsed?.ok ? <Money value={parsed.value} currency={account?.currency} /> : 'money'}
            </Button>
          </form>
        </Panel>

        <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
          <Panel title="Route" className="reveal">
            <KeyValue
              rows={[
                ['Rail', RAIL_LABELS[rail]],
                ['Timing', RAIL_NOTE[rail]],
                beneficiary ? ['Payee', beneficiary.nickname] : null,
                beneficiary ? ['Account', <span key="a" className="mono">{beneficiary.masked}</span>] : null,
                beneficiary?.bank_code ? ['IFSC', <span key="i" className="mono">{beneficiary.bank_code}</span>] : null,
                beneficiary?.swift_bic ? ['SWIFT', <span key="s" className="mono">{beneficiary.swift_bic}</span>] : null,
              ].filter(Boolean)}
            />
          </Panel>

          <Panel title="How we protect this" className="reveal">
            <ol className="small muted" style={{ paddingLeft: 18, lineHeight: 1.85, margin: 0 }}>
              <li>Your limits and the payee are checked.</li>
              <li>The amount is reserved — not yet taken.</li>
              <li>Fraud screening scores the transfer in under 50&nbsp;ms.</li>
              <li>Only then is the money moved and dispatched.</li>
            </ol>
            <p className="tiny faint" style={{ marginTop: 'var(--s-4)' }}>
              If anything fails part-way, every completed step is unwound. Money is
              never left in limbo.
            </p>
          </Panel>

          <Panel title="Payees" className="reveal">
            <p className="small muted">
              {payeeList.length} active. <Link to="/payees">Manage</Link>
            </p>
          </Panel>
        </div>
      </div>
    </Page>
  )
}
