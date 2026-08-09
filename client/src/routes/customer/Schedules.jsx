import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Page } from '../../components/AppShell'
import {
  AmountInput,
  Banner,
  Button,
  Empty,
  ErrorBanner,
  Field,
  Loading,
  Money,
  Panel,
  Select,
  Stamp,
  TextInput,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDate, formatDateTime, humanise, relativeTime } from '../../lib/format'
import { parseAmountInput } from '../../lib/money'

const FREQUENCIES = [
  { value: 'ONCE', label: 'Once' },
  { value: 'DAILY', label: 'Every day' },
  { value: 'WEEKLY', label: 'Every week' },
  { value: 'MONTHLY', label: 'Every month' },
]

const STATUS_TONE = {
  ACTIVE: 'allow',
  PAUSED: 'review',
  COMPLETED: 'neutral',
  CANCELLED: 'neutral',
  FAILED: 'block',
}

function defaultStart() {
  const when = new Date(Date.now() + 24 * 60 * 60 * 1000)
  when.setSeconds(0, 0)
  return when.toISOString().slice(0, 16)
}

export default function Schedules() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({
    account_id: '',
    beneficiary_id: '',
    amount: '',
    frequency: 'MONTHLY',
    start_at: defaultStart(),
    max_runs: '12',
  })

  const schedules = useQuery({
    queryKey: ['schedules'],
    queryFn: () => api.get('/api/schedules'),
  })
  const accounts = useQuery({ queryKey: ['accounts'], queryFn: () => api.get('/api/accounts') })
  const payees = useQuery({
    queryKey: ['beneficiaries'],
    queryFn: () => api.get('/api/beneficiaries?status=ACTIVE'),
  })

  const accountList = accounts.data?.results ?? []
  const payeeList = payees.data?.results ?? []
  const rows = schedules.data?.results ?? []

  const create = useMutation({
    mutationFn: (body) => api.post('/api/schedules', body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['schedules'] })
      toast.ok('Standing order created')
      setOpen(false)
    },
  })

  const update = useMutation({
    mutationFn: ({ id, status }) => api.patch(`/api/schedules/${id}`, { status }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['schedules'] }),
  })

  const cancel = useMutation({
    mutationFn: (id) => api.del(`/api/schedules/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['schedules'] })
      toast.ok('Standing order cancelled')
    },
  })

  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value })
  const parsed = form.amount ? parseAmountInput(form.amount) : null

  function submit(event) {
    event.preventDefault()
    if (!parsed?.ok) return
    const account = accountList.find((a) => a.id === form.account_id) ?? accountList[0]
    const payee = payeeList.find((b) => b.id === form.beneficiary_id) ?? payeeList[0]
    if (!account || !payee) return

    create.mutate({
      account_id: account.id,
      beneficiary_id: payee.id,
      amount: parsed.value,
      currency: account.currency,
      rail: payee.beneficiary_type === 'INTERNAL' ? 'INTERNAL' : 'DOMESTIC',
      frequency: form.frequency,
      start_at: new Date(form.start_at).toISOString(),
      max_runs: Number(form.max_runs) || null,
    })
  }

  return (
    <Page
      title="Standing orders"
      subtitle="Transfers that run on a schedule. Each one is screened like any other."
      actions={
        <Button variant="primary" onClick={() => setOpen((value) => !value)}>
          {open ? 'Close' : 'New standing order'}
        </Button>
      }
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        {open && (
          <Panel title="New standing order" tone="raised" ticks className="reveal">
            <form className="stack" onSubmit={submit}>
              <div className="grid-2">
                <Field label="From">
                  {(id) => (
                    <Select
                      id={id}
                      value={form.account_id || accountList[0]?.id || ''}
                      onChange={set('account_id')}
                      options={accountList.map((item) => ({
                        value: item.id,
                        label: item.account_number,
                      }))}
                    />
                  )}
                </Field>
                <Field label="To">
                  {(id) => (
                    <Select
                      id={id}
                      value={form.beneficiary_id || payeeList[0]?.id || ''}
                      onChange={set('beneficiary_id')}
                      options={payeeList.map((item) => ({
                        value: item.id,
                        label: `${item.nickname} — ${item.masked}`,
                      }))}
                    />
                  )}
                </Field>
              </div>

              <Field label="Amount" error={parsed && !parsed.ok ? parsed.reason : null}>
                {(id) => (
                  <AmountInput
                    id={id}
                    value={form.amount}
                    placeholder="0.00"
                    invalid={Boolean(parsed && !parsed.ok)}
                    onChange={set('amount')}
                  />
                )}
              </Field>

              <div className="grid-3">
                <Field label="How often">
                  {(id) => (
                    <Select
                      id={id}
                      value={form.frequency}
                      onChange={set('frequency')}
                      options={FREQUENCIES}
                    />
                  )}
                </Field>
                <Field label="First run">
                  {(id) => (
                    <TextInput
                      id={id}
                      type="datetime-local"
                      value={form.start_at}
                      onChange={set('start_at')}
                    />
                  )}
                </Field>
                <Field label="Stop after" hint="runs">
                  {(id) => (
                    <TextInput
                      id={id}
                      mono
                      inputMode="numeric"
                      value={form.max_runs}
                      onChange={set('max_runs')}
                    />
                  )}
                </Field>
              </div>

              <ErrorBanner error={create.error} title="Could not create it" />

              <Banner tone="info" title="Why a stopping condition is required">
                A recurring transfer with no end date runs forever. We ask for an end
                date or a run count so that cannot happen by accident.
              </Banner>

              <Button
                type="submit"
                variant="primary"
                busy={create.isPending}
                disabled={!parsed?.ok || payeeList.length === 0}
              >
                Create standing order
              </Button>
            </form>
          </Panel>
        )}

        <ErrorBanner error={schedules.error} title="Could not load your standing orders" />

        <Panel flush tone="framed">
          {schedules.isLoading ? (
            <Loading rows={4} />
          ) : rows.length === 0 ? (
            <Empty mark="[ ↻ ]" title="No standing orders">
              Set up a repeating transfer for rent, fees or savings.
            </Empty>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Frequency</th>
                    <th className="num">Amount</th>
                    <th>Next run</th>
                    <th className="num">Completed</th>
                    <th>Ends</th>
                    <th>Status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((schedule) => (
                    <tr key={schedule.id}>
                      <td>{humanise(schedule.frequency)}</td>
                      <td className="num">
                        <Money value={schedule.amount} currency={schedule.currency} />
                      </td>
                      <td className="tiny">
                        {formatDateTime(schedule.next_run_at)}
                        <div className="faint">{relativeTime(schedule.next_run_at)}</div>
                      </td>
                      <td className="num mono tiny">
                        {schedule.runs_completed}
                        {schedule.max_runs ? ` / ${schedule.max_runs}` : ''}
                      </td>
                      <td className="tiny muted">
                        {schedule.end_date ? formatDate(schedule.end_date) : '—'}
                      </td>
                      <td>
                        <Stamp tone={STATUS_TONE[schedule.status] ?? 'neutral'}>
                          {schedule.status}
                        </Stamp>
                        {schedule.consecutive_failures > 0 && (
                          <div className="tiny" style={{ color: 'var(--block)', marginTop: 4 }}>
                            {schedule.consecutive_failures} failed
                          </div>
                        )}
                      </td>
                      <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                        {['ACTIVE', 'PAUSED'].includes(schedule.status) && (
                          <>
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() =>
                                update.mutate({
                                  id: schedule.id,
                                  status: schedule.status === 'ACTIVE' ? 'PAUSED' : 'ACTIVE',
                                })
                              }
                            >
                              {schedule.status === 'ACTIVE' ? 'Pause' : 'Resume'}
                            </Button>{' '}
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => cancel.mutate(schedule.id)}
                            >
                              Cancel
                            </Button>
                          </>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </Page>
  )
}
