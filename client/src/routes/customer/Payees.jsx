import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
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

const TYPES = [
  { value: 'INTERNAL', label: 'Within IND Bank' },
  { value: 'DOMESTIC', label: 'Another Indian bank' },
  { value: 'INTERNATIONAL', label: 'Overseas' },
]

export default function Payees() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({
    nickname: '',
    beneficiary_type: 'DOMESTIC',
    account_number: '',
    bank_code: '',
    swift_bic: '',
    country: 'IN',
    currency: 'INR',
  })

  const payees = useQuery({
    queryKey: ['beneficiaries'],
    queryFn: () => api.get('/api/beneficiaries'),
  })

  const add = useMutation({
    mutationFn: (body) => api.post('/api/beneficiaries', body),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ['beneficiaries'] })
      toast.ok(`${created.nickname} added`)
      setOpen(false)
      setForm({ ...form, nickname: '', account_number: '', bank_code: '', swift_bic: '' })
    },
  })

  const remove = useMutation({
    mutationFn: (id) => api.del(`/api/beneficiaries/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['beneficiaries'] })
      toast.ok('Payee blocked')
    },
  })

  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value })
  const international = form.beneficiary_type === 'INTERNATIONAL'
  const internal = form.beneficiary_type === 'INTERNAL'
  const rows = payees.data?.results ?? []

  return (
    <Page
      title="Payees"
      subtitle="People and businesses you can send money to."
      actions={
        <Button variant="primary" onClick={() => setOpen((value) => !value)}>
          {open ? 'Close' : 'Add a payee'}
        </Button>
      }
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        {open && (
          <Panel title="New payee" tone="raised" ticks className="reveal">
            <form
              className="stack"
              onSubmit={(event) => {
                event.preventDefault()
                add.mutate(form)
              }}
            >
              <div className="grid-2">
                <Field label="Nickname">
                  {(id) => (
                    <TextInput id={id} value={form.nickname} required onChange={set('nickname')} />
                  )}
                </Field>
                <Field label="Where">
                  {(id) => (
                    <Select
                      id={id}
                      value={form.beneficiary_type}
                      onChange={set('beneficiary_type')}
                      options={TYPES}
                    />
                  )}
                </Field>
              </div>

              <Field label="Account number">
                {(id) => (
                  <TextInput
                    id={id}
                    mono
                    value={form.account_number}
                    required
                    onChange={set('account_number')}
                  />
                )}
              </Field>

              {international ? (
                <div className="grid-2">
                  <Field label="SWIFT / BIC">
                    {(id) => (
                      <TextInput
                        id={id}
                        mono
                        value={form.swift_bic}
                        required
                        placeholder="DEUTDEFF"
                        onChange={set('swift_bic')}
                      />
                    )}
                  </Field>
                  <Field label="Country">
                    {(id) => (
                      <TextInput
                        id={id}
                        mono
                        maxLength={2}
                        value={form.country === 'IN' ? '' : form.country}
                        required
                        placeholder="SG"
                        onChange={set('country')}
                      />
                    )}
                  </Field>
                </div>
              ) : internal ? (
                <Banner tone="info" title="We check this one against our books">
                  An IND Bank payee is verified as you add them: if the number
                  does not match an open account here, we say so now rather than
                  letting a transfer fail later. Transfers to them are instant
                  and free, and land in their account straight away.
                </Banner>
              ) : (
                <Field label="IFSC" hint="Eleven characters, e.g. HDFC0001234">
                  {(id) => (
                    <TextInput
                      id={id}
                      mono
                      value={form.bank_code}
                      required
                      placeholder="HDFC0001234"
                      onChange={set('bank_code')}
                    />
                  )}
                </Field>
              )}

              <ErrorBanner error={add.error} title="Could not add this payee" />

              <Banner tone="info" title="Ready to pay straight away">
                There is no waiting period. A brand-new payee is still one of the
                strongest fraud signals there is, so every transfer to them is
                screened on its own merits — rather than everyone being made to
                wait a day on the chance that one of them is a mule account.
              </Banner>

              <Button type="submit" variant="primary" busy={add.isPending}>
                Add payee
              </Button>
            </form>
          </Panel>
        )}

        <ErrorBanner error={payees.error} title="Could not load your payees" />

        <Panel flush tone="framed">
          {payees.isLoading ? (
            <Loading rows={4} />
          ) : rows.length === 0 ? (
            <Empty mark="[ + ]" title="No payees yet">
              Add someone to send money to.
            </Empty>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Nickname</th>
                    <th>Account</th>
                    <th>Where</th>
                    <th>Routing</th>
                    <th>Added</th>
                    <th>Status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((payee) => (
                    <tr key={payee.id}>
                      <td style={{ fontWeight: 600 }}>{payee.nickname}</td>
                      <td className="mono tiny">{payee.masked}</td>
                      <td className="tiny">
                        {TYPES.find((t) => t.value === payee.beneficiary_type)?.label}
                        {payee.country !== 'IN' && ` · ${payee.country}`}
                      </td>
                      <td className="mono tiny">
                        {payee.beneficiary_type === 'INTERNAL' ? (
                          payee.internal_verified ? (
                            <Stamp tone="allow">Verified</Stamp>
                          ) : (
                            <Stamp tone="review">Unverified</Stamp>
                          )
                        ) : (
                          payee.bank_code || payee.swift_bic || '—'
                        )}
                      </td>
                      <td className="tiny muted">{formatDateTime(payee.created_at)}</td>
                      <td>
                        <Stamp tone={payee.status === 'ACTIVE' ? 'allow' : 'neutral'}>
                          {payee.status}
                        </Stamp>
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        {payee.status === 'ACTIVE' && (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => remove.mutate(payee.id)}
                          >
                            Block
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <p className="tiny faint">
          Blocking a payee does not delete them. Their history stays on file so a
          past transfer can still be explained.
        </p>
      </div>
    </Page>
  )
}
