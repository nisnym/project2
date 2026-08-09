import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Button,
  Empty,
  ErrorBanner,
  Field,
  KeyValue,
  Loading,
  Panel,
  Stamp,
  Tabs,
  TextInput,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime, humanise, relativeTime } from '../../lib/format'

/**
 * "Investigate failed transfers."
 *
 * The useful thing this screen does is pivot from a failure straight to the
 * cross-service audit trace for the same correlation id. That is the difference
 * between "payments-svc logged an error" and "here is every step of what
 * happened, in order, across five services".
 */

const TABS = [
  { value: 'OPEN', label: 'Open' },
  { value: 'RESOLVED', label: 'Resolved' },
]

function Trace({ correlationId }) {
  const trace = useQuery({
    queryKey: ['audit', 'trace', correlationId],
    queryFn: () => api.get(`/api/audit/trace/${correlationId}`),
    enabled: Boolean(correlationId),
  })

  if (!correlationId) {
    return <p className="small faint">No correlation id was recorded for this failure.</p>
  }
  if (trace.isLoading) return <Loading rows={4} />
  if (trace.error) {
    return <ErrorBanner error={trace.error} title="Could not load the trace" />
  }

  const entries = trace.data?.results ?? []
  if (entries.length === 0) {
    return <p className="small faint">Nothing in the audit log for this id yet.</p>
  }

  return (
    <div>
      <div className="row row--between" style={{ marginBottom: 'var(--s-3)' }}>
        <span className="tiny faint">
          {trace.data.count} entries across {trace.data.services.length} services
        </span>
        {trace.data.span_ms !== null && (
          <span className="mono tiny">{trace.data.span_ms} ms end to end</span>
        )}
      </div>

      <ol style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {entries.map((entry) => (
          <li
            key={entry.id}
            style={{
              display: 'grid',
              gridTemplateColumns: '92px 1fr',
              gap: 'var(--s-3)',
              padding: 'var(--s-2) 0',
              borderBottom: '1px solid var(--rule)',
            }}
          >
            <span className="mono tiny faint">{entry.producer}</span>
            <div style={{ minWidth: 0 }}>
              <span className="mono tiny" style={{ fontWeight: 600 }}>
                {entry.event_type}
              </span>
              <div className="tiny faint">{formatDateTime(entry.occurred_at)}</div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  )
}

export default function Failures() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [status, setStatus] = useState('OPEN')
  const [selected, setSelected] = useState(null)
  const [note, setNote] = useState('')

  const failures = useQuery({
    queryKey: ['ops', 'failures', status],
    queryFn: () => api.get(`/api/ops/failures?status=${status}`),
    refetchInterval: status === 'OPEN' ? 20_000 : false,
  })

  const resolve = useMutation({
    mutationFn: (id) => api.post(`/api/ops/failures/${id}/resolve`, { note }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ops', 'failures'] })
      toast.ok('Case resolved')
      setSelected(null)
      setNote('')
    },
  })

  const rows = failures.data?.results ?? []

  return (
    <Page
      title="Failed transfers"
      subtitle="Every failure that needs a person. Each one links to its full cross-service trace."
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        <Tabs tabs={TABS} active={status} onChange={setStatus} />

        <ErrorBanner error={failures.error} title="Could not load failures" />

        <div className="split">
          <Panel flush tone="framed">
            {failures.isLoading ? (
              <Loading rows={6} />
            ) : rows.length === 0 ? (
              <Empty mark="[ ✓ ]" title="Nothing failing">
                No {status.toLowerCase()} cases.
              </Empty>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Type</th>
                      <th>Service</th>
                      <th>Subject</th>
                      <th>Opened</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((item) => (
                      <tr
                        key={item.id}
                        data-clickable
                        onClick={() => setSelected(item)}
                        style={
                          selected?.id === item.id
                            ? { background: 'var(--saffron-wash)' }
                            : undefined
                        }
                      >
                        <td>
                          <Stamp tone="block">{humanise(item.failure_type)}</Stamp>
                        </td>
                        <td className="mono tiny">{item.source_service}</td>
                        <td className="mono tiny truncate" style={{ maxWidth: 180 }}>
                          {item.subject_ref}
                        </td>
                        <td className="tiny muted">{relativeTime(item.opened_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          {selected ? (
            <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
              <Panel title="Case" tone="raised" ticks className="reveal">
                <KeyValue
                  rows={[
                    ['Type', humanise(selected.failure_type)],
                    ['Service', selected.source_service],
                    ['Subject', <span key="s" className="mono tiny">{selected.subject_ref}</span>],
                    ['Opened', formatDateTime(selected.opened_at)],
                    ['Correlation', <span key="c" className="mono tiny">{selected.correlation_id || '—'}</span>],
                  ]}
                />
                {selected.detail && (
                  <pre
                    className="mono tiny"
                    style={{
                      marginTop: 'var(--s-4)',
                      padding: 'var(--s-3)',
                      background: 'var(--surface-sunk)',
                      border: '1px solid var(--rule-strong)',
                      overflowX: 'auto',
                      whiteSpace: 'pre-wrap',
                    }}
                  >
                    {typeof selected.detail === 'string'
                      ? selected.detail
                      : JSON.stringify(selected.detail, null, 2)}
                  </pre>
                )}
              </Panel>

              <Panel title="What actually happened" className="reveal">
                <Trace correlationId={selected.correlation_id} />
              </Panel>

              {status === 'OPEN' && (
                <Panel title="Resolve" tone="framed" className="reveal">
                  <div className="stack">
                    <Field label="Resolution note">
                      {(id) => (
                        <TextInput
                          id={id}
                          value={note}
                          placeholder="Rail confirmed settlement out of band"
                          onChange={(event) => setNote(event.target.value)}
                        />
                      )}
                    </Field>
                    <ErrorBanner error={resolve.error} title="Could not resolve" />
                    <Button
                      variant="primary"
                      block
                      busy={resolve.isPending}
                      onClick={() => resolve.mutate(selected.id)}
                    >
                      Mark resolved
                    </Button>
                  </div>
                </Panel>
              )}
            </div>
          ) : (
            <Panel className="reveal">
              <Empty mark="[ ← ]" title="Pick a case">
                Select a failure to see its full trace across every service that
                touched it.
              </Empty>
            </Panel>
          )}
        </div>
      </div>
    </Page>
  )
}
