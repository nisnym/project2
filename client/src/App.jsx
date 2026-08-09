import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import AppShell from './components/AppShell'
import { useAuth } from './lib/authContext'
import { HOME_FOR_ROLE } from './lib/roles'

import Home from './routes/Home'
import Login from './routes/Login'
import Register from './routes/Register'
import Onboarding from './routes/Onboarding'
import Inbox from './routes/Inbox'

import Accounts from './routes/customer/Accounts'
import SendMoney from './routes/customer/SendMoney'
import AddMoney from './routes/customer/AddMoney'
import Activity from './routes/customer/Activity'
import TransactionDetail from './routes/customer/TransactionDetail'
import Payees from './routes/customer/Payees'
import Schedules from './routes/customer/Schedules'

import CaseQueue from './routes/analyst/CaseQueue'
import CaseDetail from './routes/analyst/CaseDetail'
import RulePerformance from './routes/analyst/RulePerformance'

import ServiceHealth from './routes/ops/ServiceHealth'
import Failures from './routes/ops/Failures'
import Reports from './routes/ops/Reports'

import FraudRules from './routes/admin/FraudRules'
import Thresholds from './routes/admin/Thresholds'
import Limits from './routes/admin/Limits'
import AuditTrail from './routes/admin/AuditTrail'

/** Blocks a route until the session is known, then on role. */
function Guard({ allow, children }) {
  const { user, ready, role } = useAuth()
  const location = useLocation()

  if (!ready) {
    return (
      <div className="gate">
        <div className="label">Restoring session…</div>
      </div>
    )
  }
  if (!user) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />
  }
  // Redirect rather than show a 403: a signed-in user reaching the wrong shell
  // has almost always followed a stale link, and their own home is the useful
  // destination.
  if (allow && !allow.includes(role)) {
    return <Navigate to={HOME_FOR_ROLE[role] ?? '/accounts'} replace />
  }
  return children
}

const CUSTOMER = ['CUSTOMER']
const ANALYST = ['FRAUD_ANALYST', 'ADMIN']
const OPS = ['OPS', 'ADMIN']
const ADMIN = ['ADMIN']
const ANY = ['CUSTOMER', 'FRAUD_ANALYST', 'OPS', 'ADMIN']

/**
 * `/` is the public homepage for anyone signed out, and a redirect to their own
 * console for anyone signed in — so the same link works for a first-time visitor
 * and for someone returning to a bookmark.
 */
function Landing() {
  const { user, ready, role } = useAuth()
  if (!ready) {
    return (
      <div className="gate">
        <span className="label">Loading…</span>
      </div>
    )
  }
  if (!user) return <Home />
  return <Navigate to={HOME_FOR_ROLE[role] ?? '/accounts'} replace />
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/home" element={<Home />} />
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />

      <Route
        element={
          <Guard allow={ANY}>
            <AppShell />
          </Guard>
        }
      >
        <Route path="/inbox" element={<Inbox />} />

        {/* customer */}
        <Route path="/onboarding" element={<Guard allow={CUSTOMER}><Onboarding /></Guard>} />
        <Route path="/accounts" element={<Guard allow={CUSTOMER}><Accounts /></Guard>} />
        <Route path="/send" element={<Guard allow={CUSTOMER}><SendMoney /></Guard>} />
        <Route path="/add-money" element={<Guard allow={CUSTOMER}><AddMoney /></Guard>} />
        <Route path="/activity" element={<Guard allow={CUSTOMER}><Activity /></Guard>} />
        <Route
          path="/activity/:txnId"
          element={<Guard allow={CUSTOMER}><TransactionDetail /></Guard>}
        />
        <Route path="/payees" element={<Guard allow={CUSTOMER}><Payees /></Guard>} />
        <Route path="/schedules" element={<Guard allow={CUSTOMER}><Schedules /></Guard>} />

        {/* fraud analyst */}
        <Route path="/fraud/queue" element={<Guard allow={ANALYST}><CaseQueue /></Guard>} />
        <Route path="/fraud/cases/:caseId" element={<Guard allow={ANALYST}><CaseDetail /></Guard>} />
        <Route path="/fraud/stats" element={<Guard allow={ANALYST}><RulePerformance /></Guard>} />

        {/* operations */}
        <Route path="/ops/queues" element={<Guard allow={OPS}><ServiceHealth /></Guard>} />
        <Route path="/ops/failures" element={<Guard allow={OPS}><Failures /></Guard>} />
        <Route path="/ops/reports" element={<Guard allow={OPS}><Reports /></Guard>} />

        {/* administration */}
        <Route path="/admin/rules" element={<Guard allow={ADMIN}><FraudRules /></Guard>} />
        <Route path="/admin/thresholds" element={<Guard allow={ADMIN}><Thresholds /></Guard>} />
        <Route path="/admin/limits" element={<Guard allow={ADMIN}><Limits /></Guard>} />
        <Route path="/admin/audit" element={<Guard allow={ADMIN}><AuditTrail /></Guard>} />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
