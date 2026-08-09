import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  AmountInput,
  Banner,
  Button,
  ErrorBanner,
  Field,
  KeyValue,
  Money,
  Panel,
  Select,
  StatusStamp,
  TextInput,
  useIdempotencyKey,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { RAIL_LABELS, RAIL_NOTE } from '../../lib/format'
import { formatMoney, parseAmountInput } from '../../lib/money'

/** UC2 funding: external bank debit, debit card, or wallet. */

const RAIL_FOR_SOURCE = {
  EXTERNAL_BANK: 'BANK_DEBIT',
  DEBIT_CARD: 'CARD',
  WALLET: 'WALLET',
}

const SOURCE_LABEL = {
  EXTERNAL_BANK: 'External bank account',
  DEBIT_CARD: 'Debit card',
  WALLET: 'Wallet',
}

function AddSourceForm({ onDone }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [form, setForm] = useState({
    source_type: 'EXTERNAL_BANK',
    display_name: '',
    token: '',
  })

  const create = useMutation({
    mutationFn: (body) => api.post('/api/funding-sources', body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['funding-sources'] })
      toast.ok('Funding source added')
      onDone()
    },
  })

  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault()
        create.mutate(form)
      }}
    >
      <Field label="Type">
        {(id) => (
          <Select
            id={id}
            value={form.source_type}
            onChange={(event) => setForm({ ...form, source_type: event.target.value })}
            options={Object.entries(SOURCE_LABEL).map(([value, label]) => ({ value, label }))}
          />
        )}
      </Field>

      <Field label="Label" hint="How it appears to you">
        {(id) => (
          <TextInput
            id={id}
            value={form.display_name}
            required
            placeholder="HDFC ****4821"
            onChange={(event) => setForm({ ...form, display_name: event.target.value })}
          />
        )}
      </Field>

      <Field label="Vault token" hint="Never a card number">
        {(id) => (
          <TextInput
            id={id}
            mono
            value={form.token}
            required
            placeholder="tok_demo_9f21c4"
            onChange={(event) => setForm({ ...form, token: event.target.value })}
          />
        )}
      </Field>

      <ErrorBanner error={create.error} title="Could not add it" />

      <div className="row">
        <Button type="submit" variant="primary" busy={create.isPending}>
          Add source
        </Button>
        <Button type="button" variant="ghost" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

export default function AddMoney() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()

  const [accountId, setAccountId] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [amount, setAmount] = useState('')
  const [adding, setAdding] = useState(false)
  const [result, setResult] = useState(null)

  const [idempotencyKey, resetKey] = useIdempotencyKey()

  const accounts = useQuery({ queryKey: ['accounts'], queryFn: () => api.get('/api/accounts') })
  const sources = useQuery({
    queryKey: ['funding-sources'],
    queryFn: () => api.get('/api/funding-sources'),
  })

  const accountList = accounts.data?.results ?? []
  const sourceList = sources.data?.results ?? []
  const account = accountList.find((a) => a.id === accountId) ?? accountList[0]
  const source = sourceList.find((s) => s.id === sourceId) ?? sourceList[0]
  const rail = RAIL_FOR_SOURCE[source?.source_type] ?? 'BANK_DEBIT'

  const parsed = useMemo(() => (amount ? parseAmountInput(amount) : null), [amount])

  const fund = useMutation({
    mutationFn: (body) => api.post('/api/funding', body, { idempotencyKey }),
    onSuccess: (txn) => {
      setResult(txn)
      resetKey()
      setAmount('')
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['transactions'] })
      toast.ok(`${txn.reference} — ${txn.status.toLowerCase()}`)
    },
  })

  return (
    <Page
      title="Add money"
      subtitle="Top up from an external bank account, a debit card, or a wallet."
    >
      <div className="split">
        <Panel tone="raised" ticks className="reveal">
          {adding ? (
            <AddSourceForm onDone={() => setAdding(false)} />
          ) : sourceList.length === 0 ? (
            <div className="stack">
              <Banner tone="info" title="No funding source yet">
                Add the account or card you want to top up from. We store a vault
                token, never the card number itself.
              </Banner>
              <Button variant="primary" onClick={() => setAdding(true)}>
                Add a funding source
              </Button>
            </div>
          ) : (
            <form
              className="stack"
              onSubmit={(event) => {
                event.preventDefault()
                if (!parsed?.ok || !account || !source) return
                fund.mutate({
                  account_id: account.id,
                  funding_source_id: source.id,
                  amount: parsed.value,
                  currency: account.currency,
                  rail,
                })
              }}
            >
              <Field label="From">
                {(id) => (
                  <Select
                    id={id}
                    value={source?.id ?? ''}
                    onChange={(event) => setSourceId(event.target.value)}
                    options={sourceList.map((item) => ({
                      value: item.id,
                      label: `${item.display_name} — ${SOURCE_LABEL[item.source_type]}`,
                    }))}
                  />
                )}
              </Field>

              <Field label="Into">
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

              <Field
                label="Amount"
                error={parsed && !parsed.ok ? parsed.reason : null}
              >
                {(id) => (
                  <AmountInput
                    id={id}
                    currency={account?.currency ?? 'INR'}
                    value={amount}
                    placeholder="0.00"
                    invalid={Boolean(parsed && !parsed.ok)}
                    onChange={(event) => setAmount(event.target.value)}
                  />
                )}
              </Field>

              <ErrorBanner error={fund.error} title="Top-up not accepted" />

              <div className="row">
                <Button
                  type="submit"
                  variant="primary"
                  busy={fund.isPending}
                  disabled={!parsed?.ok || !source}
                >
                  Add money
                </Button>
                <Button type="button" variant="ghost" onClick={() => setAdding(true)}>
                  New source
                </Button>
              </div>
            </form>
          )}
        </Panel>

        <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
          {result && (
            <Panel title="Last top-up" tone="framed" className="reveal">
              <KeyValue
                rows={[
                  ['Reference', <span key="r" className="mono">{result.reference}</span>],
                  ['Amount', <Money key="a" value={result.amount} currency={result.currency} tone="credit" />],
                  ['Status', <StatusStamp key="s" status={result.status} />],
                ]}
              />
              <Button
                size="sm"
                variant="ghost"
                className="btn--block"
                style={{ marginTop: 'var(--s-4)' }}
                onClick={() => navigate(`/activity/${result.id}`)}
              >
                Track it
              </Button>
            </Panel>
          )}

          <Panel title="Route" className="reveal">
            <KeyValue
              rows={[
                ['Rail', RAIL_LABELS[rail]],
                ['Timing', RAIL_NOTE[rail]],
              ]}
            />
          </Panel>

          <Panel title="A note on screening" className="reveal">
            <p className="small muted">
              Top-ups are screened too. A first large deposit into a brand-new
              account is one of the strongest fraud signals there is, so it may be
              held for a short review.
            </p>
          </Panel>
        </div>
      </div>
    </Page>
  )
}
