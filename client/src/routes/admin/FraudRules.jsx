import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Banner,
  Button,
  ErrorBanner,
  Field,
  KeyValue,
  Loading,
  Panel,
  Select,
  Stamp,
  Stat,
  TextInput,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/format'

/**
 * "Configure fraud rules."
 *
 * The screen is built around one idea: **measure before you ship**. A rule
 * change here can decline a real customer's payment, so the dry run — which
 * replays the proposed condition against real stored feature vectors — is
 * given more space than the save button.
 *
 * Shadow mode is presented as the safe default rather than an advanced option,
 * because a rule that is being evaluated and recorded but contributes nothing
 * is how you learn what a rule will do without anyone paying for it.
 */

const MODES = [
  { value: 'SHADOW', label: 'Shadow — recorded, contributes nothing' },
  { value: 'ACTIVE', label: 'Active — counts towards the score' },
  { value: 'DISABLED', label: 'Disabled — not evaluated' },
]

function precisionLabel(precision) {
  if (precision === null || precision === undefined) return 'no data'
  return `${Math.round(precision * 100)}%`
}

function RuleEditor({ rule, onClose }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [weight, setWeight] = useState(String(rule.weight))
  const [mode, setMode] = useState(rule.mode)
  const [condition, setCondition] = useState(JSON.stringify(rule.condition, null, 2))
  const [conditionError, setConditionError] = useState(null)

  const dryRun = useMutation({
    mutationFn: (body) => api.post('/api/fraud/rules/dry-run', body),
  })

  const save = useMutation({
    mutationFn: (body) => api.patch(`/api/fraud/rules/${rule.id}`, body),
    onSuccess: (updated) => {
      queryClient.invalidateQueries({ queryKey: ['fraud', 'rules'] })
      toast.ok(`${updated.code} updated to version ${updated.version}`)
      onClose()
    },
  })

  function parseCondition() {
    try {
      const parsed = JSON.parse(condition)
      setConditionError(null)
      return parsed
    } catch (error) {
      setConditionError(`Not valid JSON: ${error.message}`)
      return null
    }
  }

  const changedMode = mode !== rule.mode
  const goingLive = changedMode && mode === 'ACTIVE'

  return (
    <Panel title={`Edit ${rule.code}`} tone="raised" ticks className="reveal">
      <div className="stack">
        <div>
          <div style={{ fontWeight: 600 }}>{rule.name}</div>
          <p className="small muted" style={{ marginTop: 4 }}>
            {rule.description}
          </p>
        </div>

        <div className="grid-2">
          <Field label="Weight" hint="0–100">
            {(id) => (
              <TextInput
                id={id}
                mono
                inputMode="numeric"
                value={weight}
                onChange={(event) => setWeight(event.target.value)}
              />
            )}
          </Field>
          <Field label="Mode">
            {(id) => (
              <Select
                id={id}
                value={mode}
                onChange={(event) => setMode(event.target.value)}
                options={MODES}
              />
            )}
          </Field>
        </div>

        {goingLive && (
          <Banner tone="warn" title="This rule will start blocking payments">
            Moving a rule out of shadow makes it count towards every score from the
            next screening onwards. Run the dry run below first.
          </Banner>
        )}

        {rule.hard_block && (
          <Banner tone="error" title="Hard block rule">
            This rule blocks outright regardless of score. Changing its weight has no
            effect; changing its condition changes who gets declined.
          </Banner>
        )}

        <Field label="Condition" error={conditionError}>
          {(id) => (
            <textarea
              id={id}
              className="textarea mono"
              style={{ minHeight: 160, fontSize: 'var(--step--2)' }}
              value={condition}
              onChange={(event) => setCondition(event.target.value)}
            />
          )}
        </Field>

        <div className="row">
          <Button
            variant="ghost"
            busy={dryRun.isPending}
            onClick={() => {
              const parsed = parseCondition()
              if (parsed) dryRun.mutate({ condition: parsed, sample_days: 30 })
            }}
          >
            Dry run against 30 days
          </Button>
        </div>

        <ErrorBanner error={dryRun.error} title="Dry run rejected" />

        {dryRun.data && (
          <Panel title="Dry run — what this would have done" tone="framed">
            <div className="stat-grid">
              <Stat label="Screenings replayed" value={dryRun.data.evaluated} />
              <Stat label="Would have fired" value={dryRun.data.would_fire} />
              <Stat
                label="Fire rate"
                value={`${(dryRun.data.fire_rate * 100).toFixed(2)}%`}
                tone={dryRun.data.fire_rate > 0.15 ? 'warn' : 'default'}
              />
              <Stat
                label="Est. precision"
                value={
                  dryRun.data.estimated_precision === null
                    ? '—'
                    : `${Math.round(dryRun.data.estimated_precision * 100)}%`
                }
                tone={
                  dryRun.data.estimated_precision !== null &&
                  dryRun.data.estimated_precision < 0.4
                    ? 'alert'
                    : 'default'
                }
              />
            </div>
            <p className="tiny faint" style={{ marginTop: 'var(--s-3)' }}>
              Overlapped {dryRun.data.overlap_with_confirmed_fraud} confirmed fraud
              cases and {dryRun.data.overlap_with_false_positives} false positives.
              {dryRun.data.estimated_precision === null &&
                ' Too few resolved cases to estimate precision — treat this as unmeasured.'}
            </p>
          </Panel>
        )}

        <ErrorBanner error={save.error} title="Could not save the rule" />

        <div className="row">
          <Button
            variant="primary"
            busy={save.isPending}
            onClick={() => {
              const parsed = parseCondition()
              if (!parsed) return
              save.mutate({ weight: Number(weight), mode, condition: parsed })
            }}
          >
            Save rule
          </Button>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </div>
    </Panel>
  )
}

export default function FraudRules() {
  const [editing, setEditing] = useState(null)
  const rules = useQuery({ queryKey: ['fraud', 'rules'], queryFn: () => api.get('/api/fraud/rules') })

  const rows = rules.data?.results ?? []
  const active = rows.filter((rule) => rule.mode === 'ACTIVE')
  const shadow = rows.filter((rule) => rule.mode === 'SHADOW')

  return (
    <Page
      title="Fraud rules"
      subtitle="Every change here can decline a real payment. Measure first."
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        <div className="stat-grid reveal">
          <Stat label="Total rules" value={rows.length} />
          <Stat label="Active" value={active.length} />
          <Stat label="Shadow" value={shadow.length} />
          <Stat label="Hard blocks" value={rows.filter((r) => r.hard_block).length} tone="warn" />
        </div>

        {shadow.length === 0 && rows.length > 0 && (
          <Banner tone="warn" title="No rules in shadow mode">
            Every rule is live. There is no way to trial a change without it
            affecting customers — consider putting new rules in shadow first.
          </Banner>
        )}

        {editing && (
          <RuleEditor
            rule={editing}
            onClose={() => setEditing(null)}
            key={editing.id}
          />
        )}

        <ErrorBanner error={rules.error} title="Could not load the ruleset" />

        <Panel title="Ruleset" flush tone="framed">
          {rules.isLoading ? (
            <Loading rows={8} />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Code</th>
                    <th>Rule</th>
                    <th>Category</th>
                    <th>Mode</th>
                    <th className="num">Weight</th>
                    <th className="num">Fired</th>
                    <th>Precision</th>
                    <th>Version</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((rule) => (
                    <tr key={rule.id}>
                      <td className="mono tiny">{rule.code}</td>
                      <td>
                        <div style={{ fontWeight: 600 }}>{rule.name}</div>
                        {rule.hard_block && (
                          <Stamp tone="block">HARD BLOCK</Stamp>
                        )}
                      </td>
                      <td className="tiny muted">{rule.category}</td>
                      <td>
                        <Stamp tone={rule.mode === 'ACTIVE' ? 'allow' : 'pending'}>
                          {rule.mode}
                        </Stamp>
                      </td>
                      <td className="num mono">{rule.weight}</td>
                      <td className="num mono">{rule.stat?.fired_count ?? 0}</td>
                      <td className="mono tiny">{precisionLabel(rule.stat?.precision)}</td>
                      <td className="tiny faint">
                        v{rule.version}
                        <div>{formatDateTime(rule.updated_at)}</div>
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <Button size="sm" variant="ghost" onClick={() => setEditing(rule)}>
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

        <Panel title="How conditions are evaluated" className="reveal">
          <KeyValue
            rows={[
              ['Language', 'A whitelisted JSON expression tree — no eval, ever'],
              ['Depth limit', '6 levels'],
              ['Node limit', '60 nodes'],
              ['Validation', 'At save time, so a broken rule cannot reach screening'],
              ['Cache', 'Rules cache for 60s; saving refreshes every replica immediately'],
            ]}
          />
        </Panel>
      </div>
    </Page>
  )
}
