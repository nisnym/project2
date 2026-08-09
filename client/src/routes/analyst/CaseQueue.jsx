import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Empty,
  ErrorBanner,
  Loading,
  Money,
  Panel,
  Stamp,
  Stat,
  Tabs,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime, isOverdue, relativeTime } from '../../lib/format'

/**
 * The analyst's working queue.
 *
 * Ordered by SLA rather than by score or amount: the queue's job is to make
 * sure nothing ages out unreviewed, and a breached case is worse than a
 * high-scoring one that still has two hours on the clock.
 */

const TABS = [
  { value: 'OPEN', label: 'Open' },
  { value: 'ESCALATED', label: 'Escalated' },
  { value: 'APPROVED', label: 'Approved' },
  { value: 'REJECTED', label: 'Rejected' },
]

// fraud-svc emits CRITICAL / HIGH / MEDIUM / LOW. Omitting CRITICAL here made
// the most urgent cases fall through to the neutral grey stamp while MEDIUM
// ones rendered amber -- the queue's severity ordering read backwards.
const PRIORITY_TONE = {
  CRITICAL: 'block',
  HIGH: 'block',
  MEDIUM: 'review',
  LOW: 'neutral',
}

export default function CaseQueue() {
  const navigate = useNavigate()
  const [status, setStatus] = useState('OPEN')

  const cases = useQuery({
    queryKey: ['fraud', 'cases', status],
    queryFn: () => api.get(`/api/fraud/cases?status=${status}`),
    refetchInterval: status === 'OPEN' ? 15_000 : false,
  })

  const stats = useQuery({
    queryKey: ['fraud', 'stats'],
    queryFn: () => api.get('/api/fraud/stats?days=7'),
    refetchInterval: 30_000,
  })

  const rows = [...(cases.data?.results ?? [])].sort(
    (a, b) => new Date(a.sla_due_at) - new Date(b.sla_due_at),
  )
  const breaching = rows.filter((item) => isOverdue(item.sla_due_at)).length

  const byDecision = Object.fromEntries(
    (stats.data?.by_decision ?? []).map((row) => [row.decision, row.count]),
  )

  return (
    <Page
      title="Case queue"
      subtitle="Transfers held for review. The money is reserved until you decide."
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        <div className="stat-grid reveal">
          <Stat label="Open" value={stats.data?.open_cases ?? rows.length} />
          <Stat
            label="Breaching SLA"
            value={stats.data?.breaching_sla ?? breaching}
            tone={(stats.data?.breaching_sla ?? breaching) > 0 ? 'alert' : 'default'}
          />
          <Stat label="Screened · 7d" value={stats.data?.screened ?? '—'} />
          <Stat label="Blocked · 7d" value={byDecision.BLOCK ?? 0} />
          <Stat label="Reviewed · 7d" value={byDecision.REVIEW ?? 0} tone="warn" />
        </div>

        <Tabs tabs={TABS} active={status} onChange={setStatus} />

        <ErrorBanner error={cases.error} title="Could not load the queue" />

        <Panel flush tone="framed">
          {cases.isLoading ? (
            <Loading rows={6} />
          ) : rows.length === 0 ? (
            <Empty mark="[ ✓ ]" title="Queue is clear">
              No {status.toLowerCase()} cases.
            </Empty>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>SLA</th>
                    <th>Priority</th>
                    <th className="num">Score</th>
                    <th className="num">Amount</th>
                    <th>Reasons</th>
                    <th>Raised</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((item) => {
                    const overdue = isOverdue(item.sla_due_at)
                    return (
                      <tr
                        key={item.id}
                        data-clickable
                        onClick={() => navigate(`/fraud/cases/${item.id}`)}
                      >
                        <td>
                          <span
                            className="mono small"
                            style={{
                              color: overdue ? 'var(--block)' : 'var(--ink)',
                              fontWeight: overdue ? 700 : 400,
                            }}
                          >
                            {overdue ? 'BREACHED' : relativeTime(item.sla_due_at)}
                          </span>
                        </td>
                        <td>
                          <Stamp tone={PRIORITY_TONE[item.priority] ?? 'neutral'}>
                            {item.priority}
                          </Stamp>
                        </td>
                        <td className="num">
                          <strong className="mono">{item.score}</strong>
                        </td>
                        <td className="num">
                          <Money value={item.amount.amount} currency={item.amount.currency} />
                        </td>
                        <td>
                          <div className="row row--wrap" style={{ '--gap': '4px' }}>
                            {item.reason_codes.slice(0, 3).map((code) => (
                              <span key={code} className="stamp stamp--neutral mono">
                                {code}
                              </span>
                            ))}
                            {item.reason_codes.length > 3 && (
                              <span className="tiny faint">+{item.reason_codes.length - 3}</span>
                            )}
                          </div>
                        </td>
                        <td className="tiny muted">{formatDateTime(item.created_at)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </Page>
  )
}
