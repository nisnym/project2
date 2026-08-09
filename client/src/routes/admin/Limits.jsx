import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Banner,
  Button,
  ErrorBanner,
  Field,
  Loading,
  Money,
  Panel,
  TextInput,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/format'
import { exceeds } from '../../lib/money'

/** "Configure transaction limits." Per-tier and per-account ceilings. */

function LimitEditor({ policy, onClose }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [form, setForm] = useState({
    per_txn_max: policy.per_txn_max,
    daily_max: policy.daily_max,
    monthly_max: policy.monthly_max,
    daily_count_max: String(policy.daily_count_max),
  })

  const save = useMutation({
    mutationFn: () =>
      api.patch('/api/limit-policies', {
        id: policy.id,
        ...form,
        daily_count_max: Number(form.daily_count_max),
      }),
    onSuccess: (updated) => {
      queryClient.invalidateQueries({ queryKey: ['limit-policies'] })
      toast.ok(`Limits saved as version ${updated.version}`)
      onClose()
    },
  })

  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value })

  // The server enforces this too. Checking here is about not making someone
  // submit to learn the rule.
  const ordered =
    !exceeds(form.per_txn_max, form.daily_max) && !exceeds(form.daily_max, form.monthly_max)

  return (
    <Panel
      title={`Edit ${policy.scope} · ${policy.scope_ref} · ${policy.rail}`}
      tone="raised"
      ticks
      className="reveal"
    >
      <div className="stack">
        <div className="grid-2">
          <Field label="Per transaction">
            {(id) => <TextInput id={id} mono value={form.per_txn_max} onChange={set('per_txn_max')} />}
          </Field>
          <Field label="Per day">
            {(id) => <TextInput id={id} mono value={form.daily_max} onChange={set('daily_max')} />}
          </Field>
          <Field label="Per month">
            {(id) => <TextInput id={id} mono value={form.monthly_max} onChange={set('monthly_max')} />}
          </Field>
          <Field label="Transfers per day" hint="count">
            {(id) => (
              <TextInput
                id={id}
                mono
                inputMode="numeric"
                value={form.daily_count_max}
                onChange={set('daily_count_max')}
              />
            )}
          </Field>
        </div>

        {!ordered && (
          <Banner tone="error" title="Limits are out of order">
            Require per transaction ≤ per day ≤ per month.
          </Banner>
        )}

        <ErrorBanner error={save.error} title="Could not save the limits" />

        <div className="row">
          <Button variant="primary" busy={save.isPending} disabled={!ordered} onClick={() => save.mutate()}>
            Save limits
          </Button>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </div>
    </Panel>
  )
}

export default function Limits() {
  const [editing, setEditing] = useState(null)
  const policies = useQuery({
    queryKey: ['limit-policies'],
    queryFn: () => api.get('/api/limit-policies'),
  })

  const rows = policies.data?.results ?? []

  return (
    <Page
      title="Transaction limits"
      subtitle="Ceilings applied before a transfer is screened. Account-scoped policies override the tier default."
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        {editing && <LimitEditor policy={editing} onClose={() => setEditing(null)} key={editing.id} />}

        <ErrorBanner error={policies.error} title="Could not load limit policies" />

        <Panel title="Policies" flush tone="framed">
          {policies.isLoading ? (
            <Loading rows={5} />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Scope</th>
                    <th>Applies to</th>
                    <th>Rail</th>
                    <th className="num">Per transaction</th>
                    <th className="num">Per day</th>
                    <th className="num">Per month</th>
                    <th className="num">Count / day</th>
                    <th>Version</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((policy) => (
                    <tr key={policy.id}>
                      <td className="tiny">{policy.scope}</td>
                      <td style={{ fontWeight: 600 }}>{policy.scope_ref}</td>
                      <td className="tiny muted">{policy.rail}</td>
                      <td className="num">
                        <Money value={policy.per_txn_max} currency={policy.currency} />
                      </td>
                      <td className="num">
                        <Money value={policy.daily_max} currency={policy.currency} />
                      </td>
                      <td className="num">
                        <Money value={policy.monthly_max} currency={policy.currency} />
                      </td>
                      <td className="num mono">{policy.daily_count_max}</td>
                      <td className="tiny faint">
                        v{policy.version}
                        <div>{formatDateTime(policy.updated_at)}</div>
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <Button size="sm" variant="ghost" onClick={() => setEditing(policy)}>
                          Edit
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Banner tone="info" title="How limits are enforced">
          Budget is <em>reserved</em> inside a locked transaction before screening,
          then released if the transfer fails. That is what stops two concurrent
          transfers from each seeing the same available headroom and both passing.
        </Banner>
      </div>
    </Page>
  )
}
