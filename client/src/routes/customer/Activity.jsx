import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Button,
  Empty,
  ErrorBanner,
  Loading,
  Money,
  Panel,
  StatusStamp,
  Tabs,
  TextInput,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime, isInFlight, RAIL_LABELS } from '../../lib/format'

const FILTERS = [
  { value: 'all', label: 'All', query: '' },
  { value: 'in', label: 'Received', query: 'direction=CREDIT' },
  { value: 'out', label: 'Sent', query: 'direction=DEBIT' },
  { value: 'flight', label: 'In flight', query: 'status=INITIATED,VALIDATED,RESERVED,SCREENING,APPROVED,POSTED,DISPATCHED' },
  { value: 'review', label: 'Under review', query: 'status=UNDER_REVIEW' },
  { value: 'done', label: 'Settled', query: 'status=SETTLED' },
  { value: 'stopped', label: 'Stopped', query: 'status=BLOCKED,REJECTED,CANCELLED,FAILED' },
]

function TYPE_LABEL(txn) {
  if (txn.txn_type === 'FUNDING') return 'Top-up'
  return txn.direction === 'CREDIT' ? 'Received' : 'Transfer'
}

export default function Activity() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [filter, setFilter] = useState(
    params.get('status') === 'UNDER_REVIEW' ? 'review' : 'all',
  )
  const [search, setSearch] = useState('')

  const active = FILTERS.find((item) => item.value === filter) ?? FILTERS[0]
  const query = [active.query, search ? `q=${encodeURIComponent(search)}` : '', 'limit=50']
    .filter(Boolean)
    .join('&')

  const transactions = useQuery({
    queryKey: ['transactions', filter, search],
    queryFn: () => api.get(`/api/transactions?${query}`),
    refetchInterval: (result) =>
      result.state.data?.results?.some((txn) => isInFlight(txn.status)) ? 4000 : false,
  })

  const rows = transactions.data?.results ?? []

  return (
    <Page title="Activity" subtitle="Every transfer and top-up on your accounts.">
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        <div className="row row--between row--wrap">
          <Tabs
            tabs={FILTERS.map((item) => ({ value: item.value, label: item.label }))}
            active={filter}
            onChange={setFilter}
          />
          <TextInput
            mono
            placeholder="Search reference…"
            value={search}
            style={{ maxWidth: 240 }}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>

        <ErrorBanner error={transactions.error} title="Could not load your activity" />

        <Panel flush tone="framed">
          {transactions.isLoading ? (
            <Loading rows={6} />
          ) : rows.length === 0 ? (
            <Empty mark="— · —" title="Nothing here">
              {filter === 'all'
                ? 'You have not made any transfers yet.'
                : 'No transactions match this filter.'}
            </Empty>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Reference</th>
                    <th>Type</th>
                    <th>Counterparty</th>
                    <th>Route</th>
                    <th>When</th>
                    <th>Status</th>
                    <th className="num">Score</th>
                    <th className="num">Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((txn) => (
                    <tr key={txn.id} data-clickable onClick={() => navigate(`/activity/${txn.id}`)}>
                      <td className="mono tiny">{txn.transfer_ref ?? txn.reference}</td>
                      <td className="tiny">{TYPE_LABEL(txn)}</td>
                      <td>
                        {/* The server decides the wording, because the same
                            movement of money reads as "sent" to one owner and
                            "received" to the other. */}
                        <span className="tiny muted">{txn.counterparty?.label ?? 'To'}</span>{' '}
                        <span className="mono">{txn.counterparty?.masked ?? '—'}</span>
                      </td>
                      <td className="tiny muted">{RAIL_LABELS[txn.rail] ?? txn.rail}</td>
                      <td className="tiny muted">{formatDateTime(txn.created_at)}</td>
                      <td>
                        <StatusStamp status={txn.status} working={isInFlight(txn.status)} />
                      </td>
                      <td className="num tiny">
                        {txn.fraud_score === null || txn.fraud_score === undefined
                          ? '—'
                          : txn.fraud_score}
                      </td>
                      <td className="num">
                        <Money
                          value={txn.amount}
                          currency={txn.currency}
                          tone={txn.direction === 'CREDIT' ? 'credit' : 'debit'}
                          signed
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        {transactions.data?.has_more && (
          <div className="row" style={{ justifyContent: 'center' }}>
            <Button variant="ghost" onClick={() => transactions.refetch()}>
              Showing the most recent 50
            </Button>
          </div>
        )}
      </div>
    </Page>
  )
}
