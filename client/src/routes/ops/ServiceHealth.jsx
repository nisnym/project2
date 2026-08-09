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
import { formatDateTime, relativeTime } from '../../lib/format'

/**
 * "Monitor transfer queues."
 *
 * Served from stored snapshots rather than by fanning out live, so the page
 * still answers when a service is down — which is exactly when someone is
 * looking at it. That means every figure carries an "as of" time, and the page
 * says so rather than implying the numbers are current.
 */

const STATUS_TONE = { HEALTHY: 'allow', DEGRADED: 'review', DOWN: 'block' }

function QueueBar({ label, value, ceiling, tone = 'default' }) {
  const percent = Math.min(100, ceiling ? (value / ceiling) * 100 : 0)
  const colour =
    tone === 'alert' || percent > 80
      ? 'var(--block)'
      : percent > 50
        ? 'var(--review)'
        : 'var(--allow)'
  return (
    <div>
      <div className="row row--between">
        <span className="tiny faint">{label}</span>
        <span className="mono tiny" style={{ fontWeight: 600 }}>{value}</span>
      </div>
      <div
        style={{
          height: 5,
          marginTop: 4,
          border: '1px solid var(--rule-strong)',
          background: 'var(--surface-raised)',
        }}
      >
        <div style={{ width: `${percent}%`, height: '100%', background: colour }} />
      </div>
    </div>
  )
}

export default function ServiceHealth() {
  const health = useQuery({
    queryKey: ['ops', 'queues'],
    queryFn: () => api.get('/api/ops/queues'),
    refetchInterval: 10_000,
  })

  const services = health.data?.services ?? []
  const unhealthy = health.data?.unhealthy ?? []
  const totalPending = services.reduce((sum, item) => sum + (item.outbox_pending ?? 0), 0)
  const totalDead = services.reduce((sum, item) => sum + (item.outbox_dead ?? 0), 0)
  const totalFailed = services.reduce((sum, item) => sum + (item.failed_tasks_24h ?? 0), 0)

  return (
    <Page
      title="Service health"
      subtitle={
        health.data?.captured_at
          ? `Snapshot taken ${relativeTime(health.data.captured_at)} — figures are as of then, not live.`
          : 'Waiting for the first snapshot.'
      }
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        {unhealthy.length > 0 && (
          <Banner tone="error" title={`${unhealthy.length} service${unhealthy.length > 1 ? 's' : ''} not healthy`}>
            {unhealthy.join(', ')}. Events queue in the outbox while a service is
            down and are relayed when it returns — nothing is lost, but it does age.
          </Banner>
        )}

        <div className="stat-grid reveal">
          <Stat label="Services" value={services.length} />
          <Stat
            label="Unhealthy"
            value={unhealthy.length}
            tone={unhealthy.length ? 'alert' : 'default'}
          />
          <Stat
            label="Events pending"
            value={totalPending}
            tone={totalPending > 100 ? 'warn' : 'default'}
          />
          <Stat
            label="Events dead"
            value={totalDead}
            tone={totalDead ? 'alert' : 'default'}
            note="exhausted every retry"
          />
          <Stat
            label="Failed tasks · 24h"
            value={totalFailed}
            tone={totalFailed ? 'warn' : 'default'}
          />
        </div>

        <ErrorBanner error={health.error} title="Could not load service health" />

        {health.isLoading ? (
          <Panel><Loading rows={6} /></Panel>
        ) : services.length === 0 ? (
          <Panel>
            <Empty mark="[ ? ]" title="No snapshots yet">
              The health poller runs on a schedule. If this stays empty, check that
              the ops service has its Django Q cluster running.
            </Empty>
          </Panel>
        ) : (
          <div className="grid-3">
            {services.map((service) => (
              <Panel
                key={service.service}
                tone={service.status === 'HEALTHY' ? 'default' : 'raised'}
                ticks={service.status !== 'HEALTHY'}
                className="reveal"
              >
                <div className="row row--between">
                  <strong style={{ fontSize: 'var(--step-0)' }}>{service.service}</strong>
                  <Stamp
                    tone={STATUS_TONE[service.status] ?? 'neutral'}
                    working={service.status === 'DEGRADED'}
                  >
                    {service.status}
                  </Stamp>
                </div>

                {service.reason && (
                  <p className="tiny" style={{ color: 'var(--block)', marginTop: 6 }}>
                    {service.reason}
                  </p>
                )}

                <div className="stack" style={{ '--gap': 'var(--s-3)', marginTop: 'var(--s-4)' }}>
                  <QueueBar label="Queue depth" value={service.queue_depth ?? 0} ceiling={200} />
                  <QueueBar label="Outbox pending" value={service.outbox_pending ?? 0} ceiling={200} />
                  <QueueBar
                    label="Outbox dead"
                    value={service.outbox_dead ?? 0}
                    ceiling={20}
                    tone={service.outbox_dead ? 'alert' : 'default'}
                  />
                </div>

                <div className="tiny faint" style={{ marginTop: 'var(--s-4)' }}>
                  {service.oldest_pending_age_s
                    ? `Oldest pending event: ${Math.round(service.oldest_pending_age_s)}s`
                    : 'No backlog'}
                  <br />
                  Seen {formatDateTime(service.last_seen)}
                </div>
              </Panel>
            ))}
          </div>
        )}
      </div>
    </Page>
  )
}
