import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Banner,
  Button,
  ErrorBanner,
  Field,
  KeyValue,
  Loading,
  Money,
  Panel,
  ScoreMeter,
  Stamp,
  TextInput,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime, humanise, isOverdue, relativeTime } from '../../lib/format'

/**
 * The review screen.
 *
 * The design problem here is trust: an analyst is being asked to overrule an
 * automated decision, and they can only do that if they can see *why* it fired.
 * So every rule that contributed is listed with its weight and, crucially, its
 * historical precision — a rule that has been right 34% of the time is very
 * different evidence from one that has been right 97% of the time, and the
 * screen says so out loud.
 */

function precisionTone(precision) {
  if (precision === null || precision === undefined) return 'neutral'
  if (precision >= 0.7) return 'allow'
  if (precision >= 0.4) return 'review'
  return 'block'
}

function ReasonRow({ reason }) {
  const precision = reason.precision
  return (
    <li
      style={{
        padding: 'var(--s-3) 0',
        borderBottom: '1px solid var(--rule)',
      }}
    >
      <div className="row row--between" style={{ alignItems: 'flex-start' }}>
        <div style={{ minWidth: 0 }}>
          <span className="mono" style={{ fontWeight: 600, fontSize: 'var(--step--1)' }}>
            {reason.code}
          </span>
          <div className="small" style={{ marginTop: 2 }}>
            {reason.rule || reason.name || '—'}
          </div>
        </div>
        <div className="row" style={{ '--gap': 'var(--s-2)', flex: 'none' }}>
          {reason.weight !== undefined && (
            <span className="stamp stamp--neutral mono">+{reason.weight}</span>
          )}
          <Stamp tone={precisionTone(precision)}>
            {precision === null || precision === undefined
              ? 'no data'
              : `${Math.round(precision * 100)}% precise`}
          </Stamp>
        </div>
      </div>
      {reason.fired_count !== undefined && (
        <div className="tiny faint" style={{ marginTop: 4 }}>
          Fired {reason.fired_count} times historically
          {precision !== null && precision !== undefined && precision < 0.4 && (
            <strong style={{ color: 'var(--block)' }}>
              {' '}
              — this rule generates more false alarms than catches
            </strong>
          )}
        </div>
      )}
    </li>
  )
}

export default function CaseDetail() {
  const { caseId } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const toast = useToast()
  const [note, setNote] = useState('')

  const detail = useQuery({
    queryKey: ['fraud', 'case', caseId],
    queryFn: () => api.get(`/api/fraud/cases/${caseId}`),
  })

  const thresholds = useQuery({
    queryKey: ['fraud', 'thresholds'],
    queryFn: () => api.get('/api/fraud/thresholds'),
  })

  function afterDecision(message) {
    queryClient.invalidateQueries({ queryKey: ['fraud'] })
    toast.ok(message)
    navigate('/fraud/queue')
  }

  const approve = useMutation({
    mutationFn: () =>
      api.post(`/api/fraud/cases/${caseId}/approve`, {
        note,
        resolution: 'FALSE_POSITIVE',
      }),
    onSuccess: () => afterDecision('Released — the transfer will continue'),
  })

  const reject = useMutation({
    mutationFn: () =>
      api.post(`/api/fraud/cases/${caseId}/reject`, {
        note,
        resolution: 'CONFIRMED_FRAUD',
      }),
    onSuccess: () => afterDecision('Blocked — funds released back to the customer'),
  })

  if (detail.isLoading) return <Page title="Case"><Loading rows={8} /></Page>
  if (detail.error) {
    return (
      <Page title="Case">
        <ErrorBanner error={detail.error} title="Could not load this case" />
      </Page>
    )
  }

  const data = detail.data
  const decided = data.status !== 'OPEN' && data.status !== 'ESCALATED'
  const overdue = isOverdue(data.sla_due_at)
  const features = data.features ?? {}

  return (
    <Page
      title={`Case ${caseId.slice(0, 8)}`}
      subtitle={`Transaction ${data.transaction.txn_ref}`}
      actions={
        <Button variant="ghost" onClick={() => navigate('/fraud/queue')}>
          Back to queue
        </Button>
      }
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        {overdue && !decided && (
          <Banner tone="error" title="SLA breached">
            This case was due {relativeTime(data.sla_due_at)}. The customer's money
            has been held that whole time.
          </Banner>
        )}

        {decided && (
          <Banner tone="ok" title={`Already ${humanise(data.status).toLowerCase()}`}>
            Resolved as {humanise(data.resolution || '—')}.
          </Banner>
        )}

        <div className="split">
          <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
            <Panel title="Why this fired" tone="raised" ticks className="reveal">
              <div style={{ marginBottom: 'var(--s-5)' }}>
                <div className="row row--between" style={{ marginBottom: 'var(--s-3)' }}>
                  <span className="label">Score</span>
                  <span className="mono" style={{ fontSize: 'var(--step-2)', fontWeight: 600 }}>
                    {data.decision.score}
                  </span>
                </div>
                <ScoreMeter
                  score={data.decision.score}
                  allowBelow={thresholds.data?.allow_below ?? 40}
                  blockAt={thresholds.data?.block_at_or_above ?? 75}
                />
              </div>

              <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
                {(data.decision.reason_codes ?? []).map((reason, index) => (
                  <ReasonRow key={`${reason.code}-${index}`} reason={reason} />
                ))}
              </ul>

              {(data.decision.shadow_codes ?? []).length > 0 && (
                <div style={{ marginTop: 'var(--s-4)' }}>
                  <div className="label">Shadow rules — recorded, not counted</div>
                  <div className="row row--wrap" style={{ marginTop: 8, '--gap': '4px' }}>
                    {data.decision.shadow_codes.map((code) => (
                      <span key={code} className="stamp stamp--pending mono">
                        {code}
                      </span>
                    ))}
                  </div>
                  <p className="tiny faint" style={{ marginTop: 8 }}>
                    These rules are being trialled. They contributed nothing to the
                    score above.
                  </p>
                </div>
              )}
            </Panel>

            <Panel title="Signals at the time of screening" className="reveal">
              <KeyValue
                rows={Object.entries(features)
                  .filter(([, value]) => typeof value !== 'object')
                  .map(([key, value]) => [
                    humanise(key),
                    <span key={key} className="mono tiny">
                      {String(value)}
                    </span>,
                  ])}
              />
            </Panel>
          </div>

          <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
            <Panel tone="raised" ticks className="reveal">
              <div className="label">Amount held</div>
              <div style={{ marginTop: 6 }}>
                <Money
                  value={data.transaction.amount.amount}
                  currency={data.transaction.amount.currency}
                  size="display"
                />
              </div>
              <div className="row" style={{ marginTop: 'var(--s-4)', '--gap': 'var(--s-2)' }}>
                <Stamp tone={data.priority === 'HIGH' ? 'block' : 'review'}>
                  {data.priority}
                </Stamp>
                <Stamp tone={overdue ? 'block' : 'neutral'}>
                  SLA {relativeTime(data.sla_due_at)}
                </Stamp>
              </div>
            </Panel>

            <Panel title="Case" className="reveal">
              <KeyValue
                rows={[
                  ['Status', <Stamp key="s" tone={decided ? 'neutral' : 'review'}>{data.status}</Stamp>],
                  ['Rail', data.transaction.rail],
                  ['Due', formatDateTime(data.sla_due_at)],
                  ['Screened in', <span key="l" className="mono">{data.decision.latency_ms} ms</span>],
                  ['Ruleset', <span key="r" className="mono tiny">{data.decision.ruleset_version}</span>],
                ]}
              />
            </Panel>

            {!decided && (
              <Panel title="Your decision" tone="framed" className="reveal">
                <div className="stack">
                  <Field label="Note" hint="Recorded against the case">
                    {(id) => (
                      <TextInput
                        id={id}
                        value={note}
                        placeholder="Spoke to customer, payee confirmed"
                        onChange={(event) => setNote(event.target.value)}
                      />
                    )}
                  </Field>

                  <ErrorBanner error={approve.error || reject.error} title="Could not record that" />

                  <Button
                    variant="go"
                    block
                    busy={approve.isPending}
                    onClick={() => approve.mutate()}
                  >
                    Release the transfer
                  </Button>
                  <Button
                    variant="danger"
                    block
                    busy={reject.isPending}
                    onClick={() => reject.mutate()}
                  >
                    Confirm fraud &amp; block
                  </Button>

                  <p className="tiny faint">
                    Either way, the rules that fired here have their precision
                    updated — which is how the false-positive rate comes down over
                    time.
                  </p>
                </div>
              </Panel>
            )}
          </div>
        </div>
      </div>
    </Page>
  )
}
