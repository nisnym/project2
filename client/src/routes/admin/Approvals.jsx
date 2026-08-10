import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Banner,
  Button,
  Empty,
  ErrorBanner,
  KeyValue,
  Loading,
  Panel,
  Stamp,
  Tabs,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime, relativeTime } from '../../lib/format'
import { ROLE_LABEL } from '../../lib/roles'

/**
 * The four-eyes queue.
 *
 * The one thing this screen must never do is let someone approve their own
 * request. The server refuses it outright; the UI greys the buttons out so the
 * refusal is never a surprise, and says why — an approver who does not
 * understand the rule will work around it by asking a colleague to click.
 */

const FILTERS = [
  { value: 'PENDING', label: 'Awaiting approval' },
  { value: 'APPLIED', label: 'Approved' },
  { value: 'REJECTED,WITHDRAWN,FAILED', label: 'Closed' },
]

const STATUS_TONE = {
  PENDING: 'review',
  APPLIED: 'allow',
  REJECTED: 'block',
  FAILED: 'block',
  WITHDRAWN: 'neutral',
}

/** What the request will actually do, in words rather than a payload dump. */
function describe(change) {
  const payload = change.payload ?? {}
  switch (change.action) {
    case 'ROLE_CHANGE':
      return `Give ${change.target_email} the role ${ROLE_LABEL[payload.role] ?? payload.role}.`
    case 'CREATE_STAFF':
      return `Create a ${ROLE_LABEL[payload.role] ?? payload.role} account for ${payload.email}.`
    case 'UNLOCK':
      return `Unlock ${change.target_email} so they can sign in again.`
    case 'ACTIVATE':
      return `Return ${change.target_email} to active.`
    case 'CLOSE':
      return `Close ${change.target_email} permanently.`
    case 'PASSWORD_RESET':
      return `Reset the password for ${change.target_email} and sign them out everywhere.`
    default:
      return change.action_label
  }
}

function Decision({ change, viewerId, onSecret }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const mine = change.requested_by === viewerId

  function refresh() {
    queryClient.invalidateQueries({ queryKey: ['admin-approvals'] })
    queryClient.invalidateQueries({ queryKey: ['admin-users'] })
    queryClient.invalidateQueries({ queryKey: ['admin-stats'] })
  }

  const approve = useMutation({
    mutationFn: () => api.post(`/api/admin/change-requests/${change.id}/approve`, {}),
    onSuccess: (result) => {
      refresh()
      toast.ok(`${change.action_label} applied`)
      if (result.one_time_secret?.temporary_password) {
        onSecret({
          email: change.target_email || change.payload?.email,
          password: result.one_time_secret.temporary_password,
        })
      }
    },
  })

  const reject = useMutation({
    mutationFn: (reason) =>
      api.post(`/api/admin/change-requests/${change.id}/reject`, { reason }),
    onSuccess: () => {
      refresh()
      toast.push(`${change.action_label} rejected`, 'info')
    },
  })

  const withdraw = useMutation({
    mutationFn: () => api.post(`/api/admin/change-requests/${change.id}/withdraw`, {}),
    onSuccess: () => {
      refresh()
      toast.push('Request withdrawn', 'info')
    },
  })

  if (mine) {
    return (
      <div className="stack" style={{ '--gap': 'var(--s-3)' }}>
        <Banner tone="info" title="This is your own request">
          Segregation of duties: it needs a different administrator. You can
          withdraw it if it was raised in error.
        </Banner>
        <ErrorBanner error={withdraw.error} title="Could not withdraw" />
        <div className="row">
          <Button size="sm" variant="ghost" busy={withdraw.isPending} onClick={() => withdraw.mutate()}>
            Withdraw
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="stack" style={{ '--gap': 'var(--s-3)' }}>
      <ErrorBanner error={approve.error || reject.error} title="Could not record the decision" />
      <div className="row">
        <Button variant="primary" size="sm" busy={approve.isPending} onClick={() => approve.mutate()}>
          Approve and apply
        </Button>
        <Button
          size="sm"
          variant="ghost"
          busy={reject.isPending}
          onClick={() => {
            const reason = window.prompt('Why is this being rejected?', '')
            if (reason) reject.mutate(reason)
          }}
        >
          Reject
        </Button>
      </div>
    </div>
  )
}

export default function Approvals() {
  const [filter, setFilter] = useState('PENDING')
  const [secret, setSecret] = useState(null)

  const queue = useQuery({
    queryKey: ['admin-approvals', filter],
    queryFn: () => api.get(`/api/admin/change-requests?status=${filter}`),
    refetchInterval: filter === 'PENDING' ? 15_000 : false,
  })

  const rows = queue.data?.results ?? []
  const viewerId = queue.data?.viewer_id

  return (
    <Page
      title="Approvals"
      subtitle="Privileged changes waiting for a second administrator."
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        <Tabs tabs={FILTERS} active={filter} onChange={setFilter} />

        {secret && (
          <Banner tone="warn" title={`One-time password for ${secret.email} — shown once`}>
            <p className="small" style={{ marginTop: 0 }}>
              Pass it on over a channel you trust. It is not stored in the clear,
              never reaches the audit trail, and cannot be read back.
            </p>
            <p className="mono" style={{ fontSize: '1.1rem', letterSpacing: '0.04em' }}>
              {secret.password}
            </p>
            <Button size="sm" onClick={() => setSecret(null)}>
              I have copied it
            </Button>
          </Banner>
        )}

        <ErrorBanner error={queue.error} title="Could not load the queue" />

        {queue.isLoading ? (
          <Panel tone="framed">
            <Loading rows={4} />
          </Panel>
        ) : rows.length === 0 ? (
          <Panel tone="framed">
            <Empty mark="[ ✓ ]" title="Nothing waiting">
              {filter === 'PENDING'
                ? 'No privileged changes are queued.'
                : 'No requests in this state.'}
            </Empty>
          </Panel>
        ) : (
          rows.map((change) => (
            <Panel
              key={change.id}
              tone="raised"
              ticks
              title={change.action_label}
              className="reveal"
            >
              <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
                <p className="small" style={{ margin: 0, fontWeight: 600 }}>
                  {describe(change)}
                </p>

                <KeyValue
                  rows={[
                    ['Status', <Stamp key="s" tone={STATUS_TONE[change.status]}>{change.status}</Stamp>],
                    ['Requested by', change.requested_by_email],
                    [
                      'Requested',
                      `${formatDateTime(change.requested_at)} (${relativeTime(change.requested_at)})`,
                    ],
                    change.reason ? ['Reason given', change.reason] : null,
                    change.decided_by_email ? ['Decided by', change.decided_by_email] : null,
                    change.decided_at ? ['Decided', formatDateTime(change.decided_at)] : null,
                    change.decision_reason ? ['Decision reason', change.decision_reason] : null,
                    change.error ? ['Error', <span key="e" className="mono tiny">{change.error}</span>] : null,
                    [
                      'Trace',
                      <span key="c" className="mono tiny">{change.correlation_id || '—'}</span>,
                    ],
                  ].filter(Boolean)}
                />

                {change.status === 'PENDING' && (
                  <Decision change={change} viewerId={viewerId} onSecret={setSecret} />
                )}
              </div>
            </Panel>
          ))
        )}

        <Banner tone="info" title="What this queue is for">
          Every entry here grants or restores someone's access. The administrator
          who raised it cannot approve it, and every decision — including a
          rejection — is written to the hash-chained audit log under the same
          correlation id as the request.
        </Banner>
      </div>
    </Page>
  )
}
