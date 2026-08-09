import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  Banner,
  Button,
  Empty,
  ErrorBanner,
  Field,
  Loading,
  Panel,
  Select,
  Stamp,
  TextInput,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/format'

/**
 * Reports run on the queue, not in the request.
 *
 * A report that scans thirty days of failures is not something to hold an HTTP
 * connection open for, so the POST returns 202 with a poll URL and this screen
 * follows it. That is also why the status can be FAILED — worth showing rather
 * than spinning forever.
 */

const TYPES = [
  { value: 'SERVICE_HEALTH', label: 'Service health' },
  { value: 'FAILURE_SUMMARY', label: 'Failure summary' },
]

const STATUS_TONE = {
  QUEUED: 'pending',
  RUNNING: 'live',
  READY: 'allow',
  FAILED: 'block',
}

function ResultView({ result }) {
  if (!result || Object.keys(result).length === 0) {
    return <p className="small faint">No rows.</p>
  }
  if (result.error) {
    return <Banner tone="error" title="The report failed">{result.error}</Banner>
  }

  if (Array.isArray(result.services)) {
    return (
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Service</th>
              <th>Status</th>
              <th className="num">Queue</th>
              <th>Captured</th>
            </tr>
          </thead>
          <tbody>
            {result.services.map((row) => (
              <tr key={row.service}>
                <td className="mono tiny">{row.service}</td>
                <td>
                  <Stamp tone={row.status === 'HEALTHY' ? 'allow' : 'block'}>{row.status}</Stamp>
                </td>
                <td className="num mono">{row.queue_depth}</td>
                <td className="tiny muted">{formatDateTime(row.captured_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <pre
      className="mono tiny"
      style={{
        padding: 'var(--s-3)',
        background: 'var(--surface-sunk)',
        border: '1px solid var(--rule-strong)',
        overflowX: 'auto',
      }}
    >
      {JSON.stringify(result, null, 2)}
    </pre>
  )
}

export default function Reports() {
  const toast = useToast()
  const [reportType, setReportType] = useState('SERVICE_HEALTH')
  const [days, setDays] = useState('7')
  const [reportId, setReportId] = useState(null)

  const create = useMutation({
    mutationFn: () =>
      api.post('/api/ops/reports', {
        report_type: reportType,
        params: reportType === 'FAILURE_SUMMARY' ? { days: Number(days) } : {},
      }),
    onSuccess: (created) => {
      setReportId(created.id)
      toast.push('Report queued', 'info')
    },
  })

  const report = useQuery({
    queryKey: ['ops', 'report', reportId],
    queryFn: () => api.get(`/api/ops/reports/${reportId}`),
    enabled: Boolean(reportId),
    refetchInterval: (query) =>
      ['READY', 'FAILED'].includes(query.state.data?.status) ? false : 1500,
  })

  return (
    <Page title="Reports" subtitle="Generated on the work queue, then polled for.">
      <div className="split split--wide-right">
        <Panel title="Run a report" tone="raised" ticks className="reveal">
          <form
            className="stack"
            onSubmit={(event) => {
              event.preventDefault()
              create.mutate()
            }}
          >
            <Field label="Report">
              {(id) => (
                <Select
                  id={id}
                  value={reportType}
                  onChange={(event) => setReportType(event.target.value)}
                  options={TYPES}
                />
              )}
            </Field>

            {reportType === 'FAILURE_SUMMARY' && (
              <Field label="Window" hint="days">
                {(id) => (
                  <TextInput
                    id={id}
                    mono
                    inputMode="numeric"
                    value={days}
                    onChange={(event) => setDays(event.target.value)}
                  />
                )}
              </Field>
            )}

            <ErrorBanner error={create.error} title="Could not queue the report" />

            <Button type="submit" variant="primary" block busy={create.isPending}>
              Generate
            </Button>
          </form>
        </Panel>

        <Panel
          title="Result"
          className="reveal"
          action={
            report.data && (
              <Stamp
                tone={STATUS_TONE[report.data.status] ?? 'neutral'}
                working={['QUEUED', 'RUNNING'].includes(report.data.status)}
              >
                {report.data.status}
              </Stamp>
            )
          }
        >
          {!reportId ? (
            <Empty mark="[ ▤ ]" title="No report yet">
              Choose a report and generate it.
            </Empty>
          ) : report.isLoading || ['QUEUED', 'RUNNING'].includes(report.data?.status) ? (
            <Loading rows={5} />
          ) : (
            <>
              {report.data?.rows !== null && report.data?.rows !== undefined && (
                <p className="label" style={{ marginBottom: 'var(--s-3)' }}>
                  {report.data.rows} rows
                </p>
              )}
              <ResultView result={report.data?.result} />
            </>
          )}
        </Panel>
      </div>
    </Page>
  )
}
