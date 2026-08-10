import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
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
  Select,
  Stamp,
  Stat,
  TextInput,
  useToast,
} from '../../components/kit'
import { api } from '../../lib/api'
import { formatDateTime, humanise, relativeTime } from '../../lib/format'
import { ROLE_LABEL } from '../../lib/roles'

/**
 * The user directory.
 *
 * The screen is deliberately blunt about which buttons take effect now and
 * which go to a queue. An administrator who cannot tell the difference will
 * eventually assume a dangerous change has been applied when it has not — or,
 * worse, the other way round.
 */

const ROLE_TONE = {
  ADMIN: 'block',
  OPS: 'review',
  FRAUD_ANALYST: 'review',
  CUSTOMER: 'neutral',
}

const STATUS_TONE = {
  ACTIVE: 'allow',
  PENDING: 'pending',
  LOCKED: 'block',
  CLOSED: 'neutral',
}

/** Actions that apply the moment they are clicked, and those that do not. */
const IMMEDIATE = {
  LOCK: {
    label: 'Lock now',
    note: 'Suspends the account and signs them out everywhere, immediately.',
  },
  REVOKE_SESSIONS: {
    label: 'Sign out everywhere',
    note: 'Kills every refresh token. Access tokens expire within 15 minutes.',
  },
}

const STAGED = {
  UNLOCK: { label: 'Unlock', note: 'Restores sign-in.' },
  ACTIVATE: { label: 'Reactivate', note: 'Returns the account to active.' },
  CLOSE: { label: 'Close', note: 'Permanent offboarding.' },
  PASSWORD_RESET: {
    label: 'Reset password',
    note: 'Issues a one-time password, shown to whoever approves it.',
  },
}

function SecretReveal({ secret, onDone }) {
  return (
    <Banner tone="warn" title="One-time password — shown once">
      <p className="small" style={{ marginTop: 0 }}>
        Pass this to the account holder over a channel you trust. It is not
        stored in the clear, does not appear in the audit trail, and cannot be
        read back.
      </p>
      <p className="mono" style={{ fontSize: '1.1rem', letterSpacing: '0.04em' }}>
        {secret}
      </p>
      <Button size="sm" onClick={onDone}>
        I have copied it
      </Button>
    </Banner>
  )
}

function ActionButton({ user, action, spec, staged, onDone }) {
  const queryClient = useQueryClient()
  const toast = useToast()

  const run = useMutation({
    mutationFn: (reason) =>
      api.post(`/api/admin/users/${user.id}/actions`, { action, reason }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      queryClient.invalidateQueries({ queryKey: ['admin-approvals'] })
      queryClient.invalidateQueries({ queryKey: ['admin-stats'] })
      if (result.requires_approval) {
        toast.push(`${spec.label} queued for a second administrator`, 'info')
      } else {
        toast.ok(`${spec.label} applied`)
      }
      onDone?.()
    },
  })

  function click() {
    const reason = window.prompt(
      `${spec.label} — ${user.email}\n\n${spec.note}\n\nReason (recorded in the audit trail):`,
      '',
    )
    if (reason === null) return
    run.mutate(reason)
  }

  return (
    <Button size="sm" variant={staged ? 'ghost' : 'default'} busy={run.isPending} onClick={click}>
      {spec.label}
      {staged && <span className="tiny faint"> ·&nbsp;needs approval</span>}
    </Button>
  )
}

function RoleChanger({ user, roles, onDone }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [role, setRole] = useState('')
  const [reason, setReason] = useState('')

  const request = useMutation({
    mutationFn: () =>
      api.post(`/api/admin/users/${user.id}/actions`, {
        action: 'ROLE_CHANGE',
        role,
        reason,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-approvals'] })
      queryClient.invalidateQueries({ queryKey: ['admin-stats'] })
      toast.push('Role change queued for a second administrator', 'info')
      setRole('')
      setReason('')
      onDone?.()
    },
  })

  return (
    <div className="stack">
      <div className="grid-2">
        <Field label="New role">
          {(id) => (
            <Select
              id={id}
              value={role}
              onChange={(event) => setRole(event.target.value)}
              options={[
                { value: '', label: 'Choose a role…' },
                ...roles
                  .filter((value) => value !== user.role)
                  .map((value) => ({ value, label: ROLE_LABEL[value] ?? value })),
              ]}
            />
          )}
        </Field>
        <Field label="Reason" hint="Recorded against the request">
          {(id) => (
            <TextInput
              id={id}
              value={reason}
              maxLength={200}
              placeholder="Joining the ops rota"
              onChange={(event) => setReason(event.target.value)}
            />
          )}
        </Field>
      </div>

      <ErrorBanner error={request.error} title="Could not queue the role change" />

      <div className="row">
        <Button
          variant="primary"
          busy={request.isPending}
          disabled={!role}
          onClick={() => request.mutate()}
        >
          Request role change
        </Button>
        <span className="tiny faint">
          Nothing changes until a different administrator approves it.
        </span>
      </div>
    </div>
  )
}

function UserDetail({ userId, roles, onClose }) {
  const [secret, setSecret] = useState(null)

  const detail = useQuery({
    queryKey: ['admin-users', userId],
    queryFn: () => api.get(`/api/admin/users/${userId}`),
  })

  if (detail.isLoading) return <Panel tone="raised" ticks><Loading rows={5} /></Panel>
  if (detail.error) {
    return <ErrorBanner error={detail.error} title="Could not load this user" />
  }

  const user = detail.data
  const staged = user.open_requests ?? []

  return (
    <Panel
      title={user.full_name || user.email}
      tone="raised"
      ticks
      className="reveal"
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        {secret && <SecretReveal secret={secret} onDone={() => setSecret(null)} />}

        <KeyValue
          rows={[
            ['Email', <span key="e" className="mono">{user.email}</span>],
            ['Role', <Stamp key="r" tone={ROLE_TONE[user.role]}>{user.role}</Stamp>],
            ['Status', <Stamp key="s" tone={STATUS_TONE[user.status]}>{user.status}</Stamp>],
            ['Phone', user.phone || '—'],
            ['Active sessions', <span key="a" className="mono">{user.active_sessions}</span>],
            ['Failed sign-ins', <span key="f" className="mono">{user.failed_logins}</span>],
            user.locked_until
              ? ['Locked until', `${formatDateTime(user.locked_until)} (${relativeTime(user.locked_until)})`]
              : null,
            ['Last signed in', formatDateTime(user.last_login_at)],
            ['Created', formatDateTime(user.created_at)],
            user.must_change_password
              ? ['Password', <Stamp key="p" tone="review">Must be changed at next sign-in</Stamp>]
              : null,
          ].filter(Boolean)}
        />

        {staged.length > 0 && (
          <Banner tone="warn" title="Already waiting for approval">
            {staged.map((change) => (
              <div key={change.id} className="small">
                {change.action_label} · requested by {change.requested_by_email}{' '}
                {relativeTime(change.requested_at)} ·{' '}
                <Link to="/admin/approvals">open the queue</Link>
              </div>
            ))}
          </Banner>
        )}

        <div>
          <div className="label" style={{ marginBottom: 'var(--s-3)' }}>
            Containment · applies immediately
          </div>
          <div className="row row--wrap">
            {Object.entries(IMMEDIATE).map(([action, spec]) => (
              <ActionButton
                key={action}
                user={user}
                action={action}
                spec={spec}
                staged={false}
                onDone={() => detail.refetch()}
              />
            ))}
          </div>
          <p className="tiny faint" style={{ marginTop: 'var(--s-3)' }}>
            Taking access away never waits for a second administrator. A control
            that delays containment is one that helps the attacker.
          </p>
        </div>

        <div>
          <div className="label" style={{ marginBottom: 'var(--s-3)' }}>
            Grants and restorations · need a second administrator
          </div>
          <div className="row row--wrap">
            {Object.entries(STAGED).map(([action, spec]) => (
              <ActionButton
                key={action}
                user={user}
                action={action}
                spec={spec}
                staged
                onDone={() => detail.refetch()}
              />
            ))}
          </div>
        </div>

        <div>
          <div className="label" style={{ marginBottom: 'var(--s-3)' }}>
            Change role
          </div>
          <RoleChanger user={user} roles={roles} onDone={() => detail.refetch()} />
        </div>

        <div className="row">
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        </div>
      </div>
    </Panel>
  )
}

function NewStaff({ staffRoles, onClose }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [form, setForm] = useState({
    email: '',
    full_name: '',
    role: staffRoles[0] ?? 'OPS',
    reason: '',
  })

  const create = useMutation({
    mutationFn: () => api.post('/api/admin/users', form),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-approvals'] })
      queryClient.invalidateQueries({ queryKey: ['admin-stats'] })
      toast.push('Staff account queued for a second administrator', 'info')
      onClose()
    },
  })

  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value })

  return (
    <Panel title="New staff account" tone="raised" ticks className="reveal">
      <form
        className="stack"
        onSubmit={(event) => {
          event.preventDefault()
          create.mutate()
        }}
      >
        <div className="grid-2">
          <Field label="Email">
            {(id) => (
              <TextInput id={id} mono type="email" required value={form.email} onChange={set('email')} />
            )}
          </Field>
          <Field label="Full name">
            {(id) => <TextInput id={id} value={form.full_name} onChange={set('full_name')} />}
          </Field>
          <Field label="Role">
            {(id) => (
              <Select
                id={id}
                value={form.role}
                onChange={set('role')}
                options={staffRoles.map((value) => ({
                  value,
                  label: ROLE_LABEL[value] ?? value,
                }))}
              />
            )}
          </Field>
          <Field label="Reason" hint="Recorded against the request">
            {(id) => (
              <TextInput id={id} maxLength={200} value={form.reason} onChange={set('reason')} />
            )}
          </Field>
        </div>

        <Banner tone="info" title="You do not set the password">
          One is generated when the request is approved and shown once to the
          approving administrator. An admin who could choose the password of an
          account they also requested would have walked around the four-eyes
          rule without ever needing it waived.
        </Banner>

        <ErrorBanner error={create.error} title="Could not queue the account" />

        <div className="row">
          <Button type="submit" variant="primary" busy={create.isPending}>
            Request staff account
          </Button>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </form>
    </Panel>
  )
}

export default function Users() {
  const [search, setSearch] = useState('')
  const [role, setRole] = useState('')
  const [status, setStatus] = useState('')
  const [selected, setSelected] = useState(null)
  const [creating, setCreating] = useState(false)

  const query = [
    search ? `q=${encodeURIComponent(search)}` : '',
    role ? `role=${role}` : '',
    status ? `status=${status}` : '',
    'limit=100',
  ]
    .filter(Boolean)
    .join('&')

  const users = useQuery({
    queryKey: ['admin-users', query],
    queryFn: () => api.get(`/api/admin/users?${query}`),
  })
  const stats = useQuery({
    queryKey: ['admin-stats'],
    queryFn: () => api.get('/api/admin/stats'),
  })

  const rows = users.data?.results ?? []
  const roles = users.data?.roles ?? []
  const staffRoles = users.data?.staff_roles ?? []
  const counts = stats.data ?? {}

  return (
    <Page
      title="Users"
      subtitle="Everyone who can sign in, and what they are allowed to do."
      actions={
        <Button variant="primary" onClick={() => setCreating((value) => !value)}>
          {creating ? 'Close' : 'New staff account'}
        </Button>
      }
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        <div className="grid-4">
          <Stat label="Administrators" value={counts.by_role?.ADMIN ?? '—'} />
          <Stat label="Staff" value={
            (counts.by_role?.OPS ?? 0) + (counts.by_role?.FRAUD_ANALYST ?? 0)
          } />
          <Stat label="Customers" value={counts.by_role?.CUSTOMER ?? '—'} />
          <Stat
            label="Awaiting approval"
            value={counts.pending_approvals ?? 0}
            tone={counts.pending_approvals ? 'warn' : 'default'}
            note={counts.pending_approvals ? 'in the queue' : 'nothing pending'}
          />
        </div>

        {creating && <NewStaff staffRoles={staffRoles} onClose={() => setCreating(false)} />}

        {selected && (
          <UserDetail
            key={selected}
            userId={selected}
            roles={roles}
            onClose={() => setSelected(null)}
          />
        )}

        <Panel tone="framed" flush>
          <div className="row row--between row--wrap" style={{ padding: 'var(--s-4)' }}>
            <TextInput
              placeholder="Search name or email…"
              value={search}
              style={{ maxWidth: 260 }}
              onChange={(event) => setSearch(event.target.value)}
            />
            <div className="row">
              <Select
                value={role}
                onChange={(event) => setRole(event.target.value)}
                options={[
                  { value: '', label: 'All roles' },
                  ...roles.map((value) => ({ value, label: ROLE_LABEL[value] ?? value })),
                ]}
              />
              <Select
                value={status}
                onChange={(event) => setStatus(event.target.value)}
                options={[
                  { value: '', label: 'All statuses' },
                  ...(users.data?.statuses ?? []).map((value) => ({
                    value,
                    label: humanise(value),
                  })),
                ]}
              />
            </div>
          </div>

          <ErrorBanner error={users.error} title="Could not load the directory" />

          {users.isLoading ? (
            <Loading rows={6} />
          ) : rows.length === 0 ? (
            <Empty mark="[ · ]" title="Nobody matches">
              Try a broader search.
            </Empty>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Email</th>
                    <th>Role</th>
                    <th>Status</th>
                    <th className="num">Sessions</th>
                    <th>Last signed in</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((user) => (
                    <tr
                      key={user.id}
                      data-clickable
                      onClick={() => setSelected(user.id)}
                    >
                      <td style={{ fontWeight: 600 }}>{user.full_name || '—'}</td>
                      <td className="mono tiny">{user.email}</td>
                      <td>
                        <Stamp tone={ROLE_TONE[user.role]}>{user.role}</Stamp>
                      </td>
                      <td>
                        <Stamp tone={STATUS_TONE[user.status]}>
                          {user.is_locked ? 'LOCKED' : user.status}
                        </Stamp>
                      </td>
                      <td className="num mono tiny">
                        {user.failed_logins > 0 ? `${user.failed_logins} failed` : '—'}
                      </td>
                      <td className="tiny muted">{formatDateTime(user.last_login_at)}</td>
                      <td style={{ textAlign: 'right' }}>
                        <Button size="sm" variant="ghost">
                          Manage
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Banner tone="info" title="Why some buttons queue and others do not">
          Anything that <em>grants or restores</em> access — a role, an unlock, a
          reactivation, a password reset, a new staff account — is staged for a
          second administrator, and the requester may not approve their own
          request. Anything that <em>takes access away</em> applies at once. The
          last active administrator cannot be demoted, closed or locked by
          anyone, including themselves.
        </Banner>
      </div>
    </Page>
  )
}
