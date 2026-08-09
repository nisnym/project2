import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Banner,
  Button,
  Empty,
  ErrorBanner,
  Loading,
  Money,
  Panel,
  Stat,
  StatusStamp,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime, isInFlight, relativeTime } from '../../lib/format'

export default function Accounts() {
  const navigate = useNavigate()
  const accounts = useQuery({
    queryKey: ['accounts'],
    queryFn: () => api.get('/api/accounts'),
  })

  const summary = useQuery({
    queryKey: ['transactions', 'summary'],
    queryFn: () => api.get('/api/transactions/summary'),
  })

  const recent = useQuery({
    queryKey: ['transactions', 'recent'],
    queryFn: () => api.get('/api/transactions?limit=8'),
    // Keep the dashboard live while anything is mid-flight.
    refetchInterval: (query) =>
      query.state.data?.results?.some((txn) => isInFlight(txn.status)) ? 4000 : false,
  })

  if (accounts.isLoading) return <Page title="Accounts"><Loading rows={5} /></Page>
  if (accounts.error) {
    return (
      <Page title="Accounts">
        <ErrorBanner error={accounts.error} title="Could not load your accounts" />
      </Page>
    )
  }

  const list = accounts.data?.results ?? []

  if (list.length === 0) {
    return (
      <Page title="Accounts">
        <Panel tone="raised" ticks>
          <Empty
            mark="[ · ]"
            title="No account yet"
            action={
              <Button variant="primary" onClick={() => navigate('/onboarding')}>
                Open an account
              </Button>
            }
          >
            Complete your application and we will open your first account.
          </Empty>
        </Panel>
      </Page>
    )
  }

  const needsReview = summary.data?.needs_review ?? 0
  const inFlight = summary.data?.in_flight ?? 0

  return (
    <Page
      title="Accounts"
      subtitle="Balances are read from the ledger, not from a cached copy."
      actions={
        <>
          <Button onClick={() => navigate('/add-money')}>Add money</Button>
          <Button variant="primary" onClick={() => navigate('/send')}>
            Send money
          </Button>
        </>
      }
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        {needsReview > 0 && (
          <Banner tone="warn" title={`${needsReview} transfer${needsReview > 1 ? 's' : ''} under review`}>
            Our fraud team is checking {needsReview > 1 ? 'these' : 'this'}. The money is
            held, not taken — you will be told either way.{' '}
            <Link to="/activity?status=UNDER_REVIEW">See which</Link>
          </Banner>
        )}

        {list.map((account) => (
          <Panel key={account.id} tone="raised" ticks className="reveal">
            <div className="row row--between row--wrap" style={{ '--gap': 'var(--s-5)' }}>
              <div>
                <div className="label">
                  {account.account_type} · {account.tier}
                </div>
                <div className="mono" style={{ fontSize: 'var(--step-1)', marginTop: 6 }}>
                  {account.account_number}
                </div>
                <div className="tiny faint" style={{ marginTop: 4 }}>
                  {account.ifsc} · opened {formatDateTime(account.opened_at)}
                </div>
              </div>

              <div style={{ textAlign: 'right' }}>
                <div className="label">Available</div>
                <Money
                  value={account.balance.available}
                  currency={account.currency}
                  size="display"
                />
                {account.balance.held !== '0.0000' && Number(account.balance.held) > 0 && (
                  <div className="small" style={{ marginTop: 8, color: 'var(--review)' }}>
                    <Money value={account.balance.held} currency={account.currency} /> held
                    pending review
                  </div>
                )}
                {account.balance.stale && (
                  <div className="tiny faint" style={{ marginTop: 6 }}>
                    Ledger unreachable — showing last known balance
                  </div>
                )}
              </div>
            </div>
          </Panel>
        ))}

        <div className="stat-grid reveal">
          <Stat label="In flight" value={inFlight} tone={inFlight ? 'warn' : 'default'} />
          <Stat label="Under review" value={needsReview} tone={needsReview ? 'alert' : 'default'} />
          <Stat
            label="Settled"
            value={summary.data?.by_status?.SETTLED?.count ?? 0}
          />
          <Stat
            label="Blocked"
            value={summary.data?.by_status?.BLOCKED?.count ?? 0}
          />
        </div>

        <Panel
          title="Recent activity"
          flush
          className="reveal"
          action={<Link to="/activity" className="tiny">View all</Link>}
        >
          {recent.isLoading ? (
            <Loading rows={4} />
          ) : (recent.data?.results ?? []).length === 0 ? (
            <Empty mark="— · —" title="Nothing yet">
              Your transfers and top-ups will appear here.
            </Empty>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Reference</th>
                    <th>To / from</th>
                    <th>When</th>
                    <th>Status</th>
                    <th className="num">Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {recent.data.results.map((txn) => (
                    <tr
                      key={txn.id}
                      data-clickable
                      onClick={() => navigate(`/activity/${txn.id}`)}
                    >
                      <td className="mono tiny">{txn.reference}</td>
                      <td>{txn.beneficiary_masked || txn.rail}</td>
                      <td className="tiny muted">{relativeTime(txn.created_at)}</td>
                      <td>
                        <StatusStamp status={txn.status} working={isInFlight(txn.status)} />
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
      </div>
    </Page>
  )
}
