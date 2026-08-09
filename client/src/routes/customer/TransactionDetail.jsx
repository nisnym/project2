import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Banner,
  Button,
  ErrorBanner,
  KeyValue,
  Loading,
  Money,
  Panel,
  Stamp,
  StatusStamp,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime, formatTime, humanise, isInFlight, RAIL_LABELS } from '../../lib/format'

/**
 * The saga, shown to the customer.
 *
 * Each step is a real distributed transaction step with a real compensation.
 * Exposing them is unusual for a retail banking app, and deliberate: when a
 * transfer stops, "we could not reserve your funds" is a fact the customer can
 * act on, where "something went wrong" is not.
 */

const STEP_LABEL = {
  VALIDATE: 'Checks and limits',
  PLACE_HOLD: 'Funds reserved',
  SCREEN: 'Fraud screening',
  CAPTURE: 'Money moved',
  DISPATCH: 'Sent to the rail',
}

const STEP_TONE = {
  DONE: 'allow',
  PENDING: 'pending',
  RUNNING: 'live',
  FAILED: 'block',
  COMPENSATED: 'neutral',
  COMPENSATION_FAILED: 'block',
}

function Steps({ steps }) {
  if (!steps?.length) {
    return <p className="small muted">No steps recorded yet.</p>
  }
  return (
    <ol style={{ listStyle: 'none', margin: 0, padding: 0 }}>
      {steps.map((step, index) => (
        <li
          key={`${step.name}-${index}`}
          style={{
            display: 'grid',
            gridTemplateColumns: '28px 1fr auto',
            gap: 'var(--s-3)',
            alignItems: 'start',
            padding: 'var(--s-3) 0',
            borderBottom: index === steps.length - 1 ? 'none' : '1px solid var(--rule)',
          }}
        >
          <span
            className="mono tiny"
            style={{
              display: 'grid',
              placeItems: 'center',
              width: 24,
              height: 24,
              border: '2px solid var(--frame)',
              background:
                step.status === 'DONE'
                  ? 'var(--allow)'
                  : step.status === 'FAILED' || step.status === 'COMPENSATION_FAILED'
                    ? 'var(--block)'
                    : 'var(--surface-raised)',
              color:
                step.status === 'DONE' || step.status.startsWith('FAIL')
                  ? 'var(--ink-inverse)'
                  : 'var(--ink)',
            }}
          >
            {index + 1}
          </span>

          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 'var(--step--1)' }}>
              {STEP_LABEL[step.name] ?? humanise(step.name)}
            </div>
            <div className="tiny faint" style={{ marginTop: 2 }}>
              {formatTime(step.started_at)}
              {step.attempt > 1 && ` · attempt ${step.attempt}`}
            </div>
            {step.error && (
              <div className="tiny" style={{ color: 'var(--block)', marginTop: 4 }}>
                {step.error}
              </div>
            )}
          </div>

          <Stamp tone={STEP_TONE[step.status] ?? 'neutral'} working={step.status === 'RUNNING'}>
            {humanise(step.status)}
          </Stamp>
        </li>
      ))}
    </ol>
  )
}

export default function TransactionDetail() {
  const { txnId } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()

  const txn = useQuery({
    queryKey: ['transaction', txnId],
    queryFn: () => api.get(`/api/transactions/${txnId}`),
    refetchInterval: (result) => (isInFlight(result.state.data?.status) ? 3000 : false),
  })

  const cancel = useMutation({
    mutationFn: () => api.post(`/api/transactions/${txnId}/cancel`, {}),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['transaction', txnId] })
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      toast.ok('Transfer cancelled and funds released')
    },
  })

  if (txn.isLoading) return <Page title="Transfer"><Loading rows={6} /></Page>
  if (txn.error) {
    return (
      <Page title="Transfer">
        <ErrorBanner error={txn.error} title="Could not load this transfer" />
      </Page>
    )
  }

  const data = txn.data

  return (
    <Page
      title={data.reference}
      subtitle={data.txn_type === 'FUNDING' ? 'Top-up' : 'Transfer'}
      actions={
        <>
          <Button variant="ghost" onClick={() => navigate('/activity')}>
            Back
          </Button>
          {data.is_cancellable && (
            <Button variant="danger" busy={cancel.isPending} onClick={() => cancel.mutate()}>
              Cancel transfer
            </Button>
          )}
        </>
      }
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        {data.status === 'UNDER_REVIEW' && (
          <Banner tone="warn" title="Held for review">
            An analyst is checking this transfer. Your money is reserved, not sent.
            You can still cancel it.
          </Banner>
        )}
        {data.status === 'BLOCKED' && (
          <Banner tone="error" title="Blocked">
            {data.status_reason || 'This transfer was stopped by fraud screening.'} No
            money left your account.
          </Banner>
        )}
        {data.status === 'SETTLED' && (
          <Banner tone="ok" title="Settled">
            Completed {formatDateTime(data.settled_at)}.
          </Banner>
        )}

        <ErrorBanner error={cancel.error} title="Could not cancel" />

        <div className="split">
          <Panel title="Progress" className="reveal">
            <Steps steps={data.steps} />
          </Panel>

          <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
            <Panel tone="raised" ticks className="reveal">
              <div className="label">Amount</div>
              <div style={{ marginTop: 6 }}>
                <Money
                  value={data.amount}
                  currency={data.currency}
                  size="display"
                  tone={data.direction === 'CREDIT' ? 'credit' : 'debit'}
                />
              </div>
              <div style={{ marginTop: 'var(--s-4)' }}>
                <StatusStamp status={data.status} working={isInFlight(data.status)} />
              </div>
            </Panel>

            <Panel title="Details" className="reveal">
              <KeyValue
                rows={[
                  ['To', data.beneficiary_masked || '—'],
                  ['Route', RAIL_LABELS[data.rail] ?? data.rail],
                  ['Created', formatDateTime(data.created_at)],
                  ['Updated', formatDateTime(data.updated_at)],
                  data.settled_at ? ['Settled', formatDateTime(data.settled_at)] : null,
                  data.remarks ? ['Reference', data.remarks] : null,
                  data.rail_ref ? ['Rail reference', <span key="rr" className="mono tiny">{data.rail_ref}</span>] : null,
                  data.fraud_score !== null && data.fraud_score !== undefined
                    ? ['Fraud score', <span key="fs" className="mono">{data.fraud_score} / 100</span>]
                    : null,
                  data.status_reason ? ['Note', data.status_reason] : null,
                  data.schedule_id
                    ? ['From standing order', <Link key="sc" to="/schedules">View</Link>]
                    : null,
                ].filter(Boolean)}
              />
            </Panel>

            <Panel title="Trace" className="reveal">
              <p className="tiny faint">
                Quote this if you contact us — it links every service that touched
                this transfer.
              </p>
              <p className="mono tiny" style={{ marginTop: 8, wordBreak: 'break-all' }}>
                {data.correlation_id || '—'}
              </p>
            </Panel>
          </div>
        </div>
      </div>
    </Page>
  )
}
