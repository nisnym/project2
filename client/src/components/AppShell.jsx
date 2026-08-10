import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { useAuth } from '../lib/authContext'
import { ROLE_LABEL } from '../lib/roles'

/**
 * The chrome all four roles share.
 *
 * Role identity is carried by exactly two things: the `--role-accent` variable
 * and the suffix under the wordmark. Everything else is identical, which is
 * what keeps a customer screen and an analyst screen recognisably the same
 * institution while never being mistakable for one another.
 */

const ROLE_ACCENT = {
  CUSTOMER: 'var(--saffron)',
  FRAUD_ANALYST: '#9a6508',
  OPS: '#1d6079',
  ADMIN: '#5b3a86',
}

const NAV = {
  CUSTOMER: [
    {
      heading: 'Banking',
      links: [
        { to: '/accounts', label: 'Accounts' },
        { to: '/send', label: 'Send money' },
        { to: '/add-money', label: 'Add money' },
        { to: '/activity', label: 'Activity' },
      ],
    },
    {
      heading: 'Manage',
      links: [
        { to: '/payees', label: 'Payees' },
        { to: '/schedules', label: 'Standing orders' },
      ],
    },
  ],
  FRAUD_ANALYST: [
    {
      heading: 'Review',
      links: [
        { to: '/fraud/queue', label: 'Case queue' },
        { to: '/fraud/stats', label: 'Rule performance' },
      ],
    },
  ],
  OPS: [
    {
      heading: 'Monitor',
      links: [
        { to: '/ops/queues', label: 'Service health' },
        { to: '/ops/failures', label: 'Failed transfers' },
        { to: '/ops/reports', label: 'Reports' },
      ],
    },
  ],
  ADMIN: [
    {
      heading: 'Access',
      links: [
        { to: '/admin/users', label: 'Users' },
        { to: '/admin/approvals', label: 'Approvals' },
      ],
    },
    {
      heading: 'Configure',
      links: [
        { to: '/admin/rules', label: 'Fraud rules' },
        { to: '/admin/thresholds', label: 'Score thresholds' },
        { to: '/admin/limits', label: 'Transaction limits' },
      ],
    },
    {
      heading: 'Oversight',
      links: [
        { to: '/admin/audit', label: 'Audit trail' },
        { to: '/fraud/queue', label: 'Case queue' },
        { to: '/ops/queues', label: 'Service health' },
      ],
    },
  ],
}

function useUnreadCount() {
  const { data } = useQuery({
    queryKey: ['notifications', 'unread'],
    queryFn: () => api.get('/api/notifications?unread=true'),
    // No Redis means no Channels means no WebSocket (ADR-007). Polling is the
    // honest answer; 20s is well inside what a person notices.
    refetchInterval: 20_000,
    retry: false,
  })
  return data?.unread_count ?? 0
}

export default function AppShell() {
  const { user, signOut, role } = useAuth()
  const navigate = useNavigate()
  const unread = useUnreadCount()
  const groups = NAV[role] ?? []

  return (
    <div className="shell" style={{ '--role-accent': ROLE_ACCENT[role] ?? 'var(--saffron)' }}>
      <header className="masthead">
        <NavLink to="/" className="masthead__brand">
          <span className="stamp-mark" aria-hidden="true">
            IB
          </span>
          <span className="wordmark">
            <span className="wordmark__name">IND Bank</span>
            <span className="wordmark__role">{ROLE_LABEL[role] ?? 'Banking'}</span>
          </span>
        </NavLink>

        <div className="masthead__right">
          <NavLink to="/inbox" className="masthead__cell masthead__cell--hide-sm">
            Inbox
            <span className={`count-box ${unread ? '' : 'count-box--quiet'}`}>{unread}</span>
          </NavLink>

          <div className="masthead__cell masthead__cell--hide-sm">
            <span className="masthead__user">
              <strong>{user?.full_name || user?.email}</strong>
              <span className="tiny">{user?.email}</span>
            </span>
          </div>

          <button
            className="masthead__cell"
            onClick={async () => {
              await signOut()
              navigate('/login', { replace: true })
            }}
          >
            Sign out
          </button>
        </div>
      </header>

      <nav className="nav" aria-label="Primary">
        {groups.map((group) => (
          <div className="nav__group" key={group.heading}>
            <div className="nav__heading">{group.heading}</div>
            {group.links.map((link) => (
              <NavLink
                key={link.to}
                to={link.to}
                className={({ isActive }) => `nav__link ${isActive ? 'is-active' : ''}`}
              >
                {link.label}
              </NavLink>
            ))}
          </div>
        ))}

        <div className="nav__foot">
          <div className="label">Environment</div>
          <div className="tiny muted" style={{ marginTop: 6 }}>
            Simulated rails. No real money moves.
          </div>
        </div>
      </nav>

      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}

export function Page({ title, subtitle, actions, children }) {
  return (
    <div className="page">
      <div className="page__head">
        <div>
          <h1 className="page__title">{title}</h1>
          {subtitle && <p className="page__sub">{subtitle}</p>}
        </div>
        {actions && <div className="row">{actions}</div>}
      </div>
      {children}
    </div>
  )
}
