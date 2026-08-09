import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Banner,
  Button,
  Empty,
  ErrorBanner,
  Field,
  KeyValue,
  Loading,
  Panel,
  Stamp,
  Stat,
  TextInput,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/format'

/**
 * The audit trail.
 *
 * The chain verification panel is the point of the screen. An append-only log
 * is only worth having if you can prove nothing was edited, and the row hash /
 * previous hash chain lets you do exactly that. Verification is opt-in because
 * it re-hashes every row — not something a dashboard poll should trigger.
 */

export default function AuditTrail() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [filters, setFilters] = useState({ correlation_id: '', event_type: '', actor_id: '' })
  const [applied, setApplied] = useState({})
  const [selected, setSelected] = useState(null)

  const query = new URLSearchParams(
    Object.entries(applied).filter(([, value]) => value),
  ).toString()

  const logs = useQuery({
    queryKey: ['audit', 'logs', query],
    queryFn: () => api.get(`/api/audit/logs?limit=60${query ? `&${query}` : ''}`),
  })

  const stats = useQuery({ queryKey: ['audit', 'stats'], queryFn: () => api.get('/api/audit/stats') })

  const chain = useQuery({
    queryKey: ['audit', 'chain'],
    queryFn: () => api.get('/api/audit/chain'),
  })

  const verify = useMutation({
    mutationFn: () => api.get('/api/audit/chain?verify=true'),
    onSuccess: (result) => {
      queryClient.setQueryData(['audit', 'chain'], result)
      if (result.verified) toast.ok(`Chain intact — ${result.rows_checked} rows re-hashed`)
      else toast.error(`Chain broken at entry ${result.broken_at_id}`)
    },
  })

  const set = (key) => (event) => setFilters({ ...filters, [key]: event.target.value })
  const rows = logs.data?.results ?? []

  return (
    <Page
      title="Audit trail"
      subtitle="Append-only and hash-chained. Every event every service emitted."
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        <div className="stat-grid reveal">
          <Stat label="Entries" value={stats.data?.total ?? '—'} />
          <Stat
            label="Chain"
            value={
              chain.data?.verified === undefined
                ? '—'
                : chain.data.verified
                  ? 'Intact'
                  : 'BROKEN'
            }
            tone={chain.data && chain.data.verified === false ? 'alert' : 'default'}
          />
          <Stat label="Producers" value={stats.data?.by_producer?.length ?? '—'} />
          <Stat label="Event types" value={stats.data?.by_event_type?.length ?? '—'} />
        </div>

        {chain.data && chain.data.verified === false && chain.data.broken_at_id && (
          <Banner tone="error" title="Tamper detected">
            The hash chain breaks at entry {chain.data.broken_at_id}. Every entry
            after it is suspect. {chain.data.detail}
          </Banner>
        )}

        <Panel
          title="Filter"
          className="reveal"
          action={
            <Button size="sm" variant="ghost" busy={verify.isPending} onClick={() => verify.mutate()}>
              Verify chain
            </Button>
          }
        >
          <form
            className="grid-3"
            onSubmit={(event) => {
              event.preventDefault()
              setApplied(filters)
            }}
          >
            <Field label="Correlation id">
              {(id) => (
                <TextInput
                  id={id}
                  mono
                  value={filters.correlation_id}
                  placeholder="Reassembles one customer action"
                  onChange={set('correlation_id')}
                />
              )}
            </Field>
            <Field label="Event type">
              {(id) => (
                <TextInput
                  id={id}
                  mono
                  value={filters.event_type}
                  placeholder="payment.blocked"
                  onChange={set('event_type')}
                />
              )}
            </Field>
            <Field label="Actor">
              {(id) => <TextInput id={id} mono value={filters.actor_id} onChange={set('actor_id')} />}
            </Field>
            <div style={{ gridColumn: '1 / -1' }}>
              <Button type="submit" variant="primary" size="sm">
                Apply
              </Button>{' '}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  setFilters({ correlation_id: '', event_type: '', actor_id: '' })
                  setApplied({})
                }}
              >
                Clear
              </Button>
            </div>
          </form>
        </Panel>

        <ErrorBanner error={logs.error} title="Could not load the audit log" />

        <div className="split">
          <Panel title="Entries" flush tone="framed">
            {logs.isLoading ? (
              <Loading rows={8} />
            ) : rows.length === 0 ? (
              <Empty mark="[ ∅ ]" title="No matching entries" />
            ) : (
              <div className="table-wrap" style={{ maxHeight: 620, overflowY: 'auto' }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th className="num">#</th>
                      <th>Event</th>
                      <th>From</th>
                      <th>When</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((entry) => (
                      <tr
                        key={entry.id}
                        data-clickable
                        onClick={() => setSelected(entry)}
                        style={
                          selected?.id === entry.id
                            ? { background: 'var(--saffron-wash)' }
                            : undefined
                        }
                      >
                        <td className="num mono tiny">{entry.id}</td>
                        <td className="mono tiny">{entry.event_type}</td>
                        <td className="tiny muted">{entry.producer}</td>
                        <td className="tiny muted">{formatDateTime(entry.occurred_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          {selected ? (
            <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
              <Panel title={`Entry ${selected.id}`} tone="raised" ticks className="reveal">
                <KeyValue
                  rows={[
                    ['Event', <span key="e" className="mono tiny">{selected.event_type}</span>],
                    ['Producer', selected.producer],
                    ['Aggregate', `${selected.aggregate_type} ${selected.aggregate_id}`],
                    ['Actor', selected.actor_id ? `${selected.actor_type} ${selected.actor_id}` : '—'],
                    ['Occurred', formatDateTime(selected.occurred_at)],
                    ['Recorded', formatDateTime(selected.recorded_at)],
                    ['Correlation', <span key="c" className="mono tiny">{selected.correlation_id || '—'}</span>],
                  ]}
                />
              </Panel>

              <Panel title="Payload" className="reveal">
                <pre
                  className="mono tiny"
                  style={{
                    padding: 'var(--s-3)',
                    background: 'var(--surface-sunk)',
                    border: '1px solid var(--rule-strong)',
                    overflowX: 'auto',
                    maxHeight: 300,
                  }}
                >
                  {JSON.stringify(selected.payload, null, 2)}
                </pre>
              </Panel>

              <Panel title="Chain position" className="reveal">
                <div className="stack" style={{ '--gap': 'var(--s-2)' }}>
                  <div>
                    <div className="label">Previous hash</div>
                    <div className="mono tiny" style={{ wordBreak: 'break-all' }}>
                      {selected.prev_hash}
                    </div>
                  </div>
                  <div>
                    <div className="label">This row&rsquo;s hash</div>
                    <div className="mono tiny" style={{ wordBreak: 'break-all' }}>
                      {selected.row_hash}
                    </div>
                  </div>
                </div>
                <p className="tiny faint" style={{ marginTop: 'var(--s-3)' }}>
                  Each row hashes its own contents together with the previous row&rsquo;s
                  hash. Editing any entry invalidates every hash after it.
                </p>
              </Panel>
            </div>
          ) : (
            <Panel className="reveal">
              <Empty mark="[ ← ]" title="Pick an entry">
                Select an entry to see its payload and its position in the hash chain.
              </Empty>
              {chain.data && (
                <div style={{ padding: '0 var(--s-4) var(--s-4)' }}>
                  <KeyValue
                    rows={[
                      ['Last verified', chain.data.finished_at ? formatDateTime(chain.data.finished_at) : 'never'],
                      ['Rows checked', chain.data.rows_checked ?? '—'],
                      [
                        'Result',
                        <Stamp key="r" tone={chain.data.verified ? 'allow' : 'review'}>
                          {chain.data.verified ? 'INTACT' : 'UNVERIFIED'}
                        </Stamp>,
                      ],
                    ]}
                  />
                </div>
              )}
            </Panel>
          )}
        </div>
      </div>
    </Page>
  )
}
