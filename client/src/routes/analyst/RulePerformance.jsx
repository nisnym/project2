import { useQuery } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Banner,
  Empty,
  ErrorBanner,
  Loading,
  Panel,
  Stamp,
  Stat,
} from '../../components/kit'
import { api } from '../../lib/api'

/**
 * Rule performance — the false-positive reduction loop, made visible.
 *
 * Sorted worst-precision first. The screen is trying to answer one question:
 * which rule is wasting the most analyst time, and should it be demoted to
 * shadow mode?
 */

function PrecisionBar({ precision }) {
  if (precision === null || precision === undefined) {
    return <span className="tiny faint">not enough resolved cases</span>
  }
  const percent = Math.round(precision * 100)
  const colour =
    precision >= 0.7 ? 'var(--allow)' : precision >= 0.4 ? 'var(--review)' : 'var(--block)'
  return (
    <div className="row" style={{ '--gap': 'var(--s-2)' }}>
      <div
        style={{
          width: 90,
          height: 12,
          border: '1px solid var(--rule-strong)',
          background: 'var(--surface-raised)',
        }}
      >
        <div style={{ width: `${percent}%`, height: '100%', background: colour }} />
      </div>
      <span className="mono tiny" style={{ color: colour, fontWeight: 600 }}>
        {percent}%
      </span>
    </div>
  )
}

export default function RulePerformance() {
  const rules = useQuery({ queryKey: ['fraud', 'rules'], queryFn: () => api.get('/api/fraud/rules') })
  const stats = useQuery({
    queryKey: ['fraud', 'stats', 30],
    queryFn: () => api.get('/api/fraud/stats?days=30'),
  })

  const rows = [...(rules.data?.results ?? [])].sort((a, b) => {
    const left = a.stat?.precision
    const right = b.stat?.precision
    if (left === null || left === undefined) return 1
    if (right === null || right === undefined) return -1
    return left - right
  })

  const resolutions = Object.fromEntries(
    (stats.data?.by_resolution ?? []).map((row) => [row.resolution, row.count]),
  )
  const confirmed = resolutions.CONFIRMED_FRAUD ?? 0
  const falsePositive = resolutions.FALSE_POSITIVE ?? 0
  const resolved = confirmed + falsePositive

  const noisy = rows.filter(
    (rule) =>
      rule.mode === 'ACTIVE' &&
      rule.stat?.precision !== null &&
      rule.stat?.precision !== undefined &&
      rule.stat.precision < 0.4 &&
      rule.stat.fired_count >= 5,
  )

  return (
    <Page
      title="Rule performance"
      subtitle="Which rules are earning their place, and which are just generating work."
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        <div className="stat-grid reveal">
          <Stat label="Confirmed fraud · 30d" value={confirmed} />
          <Stat label="False positives · 30d" value={falsePositive} tone={falsePositive > confirmed ? 'warn' : 'default'} />
          <Stat
            label="Overall precision"
            value={resolved ? `${Math.round((confirmed / resolved) * 100)}%` : '—'}
          />
          <Stat label="Active rules" value={rows.filter((r) => r.mode === 'ACTIVE').length} />
          <Stat label="Shadow rules" value={rows.filter((r) => r.mode === 'SHADOW').length} />
        </div>

        {noisy.length > 0 && (
          <Banner tone="warn" title={`${noisy.length} rule${noisy.length > 1 ? 's are' : ' is'} below 40% precision`}>
            {noisy.map((rule) => rule.code).join(', ')} — each of these is producing
            more false alarms than genuine catches. An administrator can move them to
            shadow mode without losing the data they generate.
          </Banner>
        )}

        <ErrorBanner error={rules.error} title="Could not load the ruleset" />

        <Panel title="Rules, worst precision first" flush tone="framed">
          {rules.isLoading ? (
            <Loading rows={8} />
          ) : rows.length === 0 ? (
            <Empty title="No rules configured" />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Code</th>
                    <th>Rule</th>
                    <th>Mode</th>
                    <th className="num">Weight</th>
                    <th className="num">Fired</th>
                    <th className="num">Caught</th>
                    <th className="num">False</th>
                    <th>Precision</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((rule) => (
                    <tr key={rule.id}>
                      <td className="mono tiny">{rule.reason_code}</td>
                      <td>
                        <div style={{ fontWeight: 600 }}>{rule.name}</div>
                        <div className="tiny faint">{rule.category}</div>
                      </td>
                      <td>
                        <Stamp tone={rule.mode === 'ACTIVE' ? 'allow' : 'pending'}>
                          {rule.mode}
                        </Stamp>
                        {rule.hard_block && (
                          <div style={{ marginTop: 4 }}>
                            <Stamp tone="block">HARD BLOCK</Stamp>
                          </div>
                        )}
                      </td>
                      <td className="num mono">{rule.weight}</td>
                      <td className="num mono">{rule.stat?.fired_count ?? 0}</td>
                      <td className="num mono" style={{ color: 'var(--allow)' }}>
                        {rule.stat?.confirmed_fraud ?? 0}
                      </td>
                      <td className="num mono" style={{ color: 'var(--block)' }}>
                        {rule.stat?.false_positive ?? 0}
                      </td>
                      <td>
                        <PrecisionBar precision={rule.stat?.precision} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <p className="tiny faint">
          Precision is confirmed fraud divided by all resolved cases where the rule
          fired. A rule with no resolved cases has no precision — that is different
          from zero, and is shown as such.
        </p>
      </div>
    </Page>
  )
}
