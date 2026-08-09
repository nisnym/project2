import { Link } from 'react-router-dom'
import '../styles/home.css'

/**
 * The public front door.
 *
 * Not a marketing page — a map. Someone arriving here (a demo audience, a new
 * developer, a stakeholder) needs to know what the four consoles are, what each
 * one can actually do, and what happens to a payment in between. So the page is
 * organised the way the system is: capabilities by role, then the path a
 * payment takes, then the decision that gets made about it.
 */

const CONSOLES = [
  {
    accent: 'var(--saffron)',
    role: 'Customer',
    title: 'Personal banking',
    blurb: 'Everything an account holder does with their money.',
    items: [
      'Open an account — identity checks, eligibility scoring, account number issued',
      'Add money from an external bank, a debit card or a wallet',
      'Send within IND Bank, across India, or internationally',
      'Manage payees, with a cooling-off period on new ones',
      'Set up standing orders — daily, weekly or monthly',
      'Follow a transfer step by step and cancel it before it leaves',
    ],
  },
  {
    accent: '#9a6508',
    role: 'Fraud analyst',
    title: 'Fraud operations',
    blurb: 'Review what the rules could not decide alone.',
    items: [
      'Work a queue ordered by SLA, not by whichever case is loudest',
      'See every rule that fired, its weight, and how often it has been right',
      'Release a transfer or confirm fraud — both update rule precision',
      'Watch which rules are generating more false alarms than catches',
    ],
  },
  {
    accent: '#1d6079',
    role: 'Operations',
    title: 'Service operations',
    blurb: 'Keep the estate moving.',
    items: [
      'Monitor queue depth and event backlog across all ten services',
      'Investigate failed transfers with the full cross-service trace',
      'Resolve failure cases with a recorded note',
      'Generate health and failure reports off the work queue',
    ],
  },
  {
    accent: '#5b3a86',
    role: 'Administrator',
    title: 'Administration',
    blurb: 'Set the policy the rest of the system enforces.',
    items: [
      'Tune fraud rules, and dry-run a change against 30 days of real traffic first',
      'Move a rule to shadow mode — evaluated and recorded, but counting for nothing',
      'Move the allow / review / block score thresholds',
      'Set per-transaction, daily and monthly limits by tier or account',
      'Search the hash-chained audit trail and verify it has not been altered',
    ],
  },
]

const JOURNEY = [
  { n: '01', name: 'Validate', text: 'Limits, payee and account status are checked, and limit budget is reserved.' },
  { n: '02', name: 'Reserve', text: 'The amount is held on the ledger. Not taken — held.' },
  { n: '03', name: 'Screen', text: 'Fraud rules score the transfer in under 50 milliseconds.' },
  { n: '04', name: 'Capture', text: 'Double-entry postings move the money for real.' },
  { n: '05', name: 'Dispatch', text: 'The instruction goes to the payment rail.' },
]

const DECISIONS = [
  {
    tone: 'allow',
    band: 'Score 0–39',
    verdict: 'Allow',
    text: 'Goes straight through. Most transfers land here.',
  },
  {
    tone: 'review',
    band: 'Score 40–74',
    verdict: 'Review',
    text: 'Money stays held while an analyst decides. Nothing is lost either way.',
  },
  {
    tone: 'block',
    band: 'Score 75–100',
    verdict: 'Block',
    text: 'Declined. The hold is released and the balance is untouched.',
  },
]

const FACTS = [
  ['10', 'independent services'],
  ['1', 'database each'],
  ['38', 'event types'],
  ['15', 'fraud rules'],
  ['0', 'shared tables'],
]

export default function Home() {
  return (
    <div className="home">
      <header className="home__bar">
        <Link to="/" className="home__brand">
          <span className="stamp-mark" aria-hidden="true">IB</span>
          <span className="wordmark">
            <span className="wordmark__name">IND Bank</span>
            <span className="wordmark__role">Payments Platform</span>
          </span>
        </Link>
        <nav className="home__bar-links">
          <Link to="/login" className="home__bar-link">Sign in</Link>
          <Link to="/register" className="home__bar-link home__bar-link--cta">
            Open an account
          </Link>
        </nav>
      </header>

      <div className="gate__rule" style={{ borderInline: 'none' }} />

      {/* ---- hero ---------------------------------------------------- */}
      <section className="home__hero">
        <div className="home__hero-copy">
          <p className="label">Retail payments · funding · fraud operations</p>
          <h1 className="home__headline">
            Money moves,
            <br />
            <em>or it doesn&rsquo;t move at all.</em>
          </h1>
          <p className="home__lede">
            A payments platform where every transfer is reserved before it is taken,
            screened before it is sent, and unwound completely if any step fails.
            Four consoles, ten services, one audit trail that cannot be edited.
          </p>
          <div className="row" style={{ marginTop: 'var(--s-5)' }}>
            <Link to="/register" className="btn btn--primary">Open an account</Link>
            <Link to="/login" className="btn">Sign in to a console</Link>
          </div>
        </div>

        <aside className="home__hero-plate panel__ticks">
          <div className="home__plate-head">
            <span className="label label--ink">Live transfer</span>
            <span className="stamp stamp--review stamp--working">Screening</span>
          </div>
          <div className="home__plate-body">
            <div className="label">Amount</div>
            <div className="money money--display" style={{ marginTop: 4 }}>
              <span className="money__mark">₹</span>62,400<span className="money__minor">.00</span>
            </div>

            <div className="home__meter" aria-hidden="true">
              {Array.from({ length: 20 }, (_, index) => (
                <span
                  key={index}
                  className="home__meter-cell"
                  data-on={index < 11}
                  style={{ animationDelay: `${index * 45}ms` }}
                />
              ))}
            </div>
            <div className="row row--between">
              <span className="label">Score 54</span>
              <span className="label">Review band</span>
            </div>

            <ul className="home__codes">
              <li><span className="mono">R005</span> New payee, large amount</li>
              <li><span className="mono">R012</span> Outside usual hours</li>
            </ul>
          </div>
        </aside>
      </section>

      {/* ---- consoles ------------------------------------------------ */}
      <section className="home__section">
        <div className="home__section-head">
          <h2 className="home__h2">Four consoles</h2>
          <p className="home__section-sub">
            One institution, four jobs. Each console shows only what that role needs
            and is allowed to see.
          </p>
        </div>

        <div className="home__consoles">
          {CONSOLES.map((console) => (
            <article
              key={console.role}
              className="home__console"
              style={{ '--accent': console.accent }}
            >
              <div className="home__console-head">
                <span className="label" style={{ color: console.accent }}>{console.role}</span>
                <h3 className="home__console-title">{console.title}</h3>
                <p className="small muted">{console.blurb}</p>
              </div>
              <ul className="home__list">
                {console.items.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </article>
          ))}
        </div>
      </section>

      {/* ---- journey ------------------------------------------------- */}
      <section className="home__section home__section--sunk">
        <div className="home__section-head">
          <h2 className="home__h2">What happens to a payment</h2>
          <p className="home__section-sub">
            Five steps, each with its own compensation. If step four fails, steps one
            to three are undone — money is never left in limbo.
          </p>
        </div>

        <ol className="home__journey">
          {JOURNEY.map((step) => (
            <li key={step.n} className="home__step">
              <span className="home__step-n mono">{step.n}</span>
              <h3 className="home__step-name">{step.name}</h3>
              <p className="tiny muted">{step.text}</p>
            </li>
          ))}
        </ol>
      </section>

      {/* ---- decisions ----------------------------------------------- */}
      <section className="home__section">
        <div className="home__section-head">
          <h2 className="home__h2">Three possible verdicts</h2>
          <p className="home__section-sub">
            Every transfer gets a score. Where it falls decides what happens next —
            and an administrator can move those lines.
          </p>
        </div>

        <div className="home__decisions">
          {DECISIONS.map((decision) => (
            <article key={decision.verdict} className={`home__decision home__decision--${decision.tone}`}>
              <span className={`stamp stamp--${decision.tone}`}>{decision.verdict}</span>
              <div className="mono tiny muted" style={{ marginTop: 'var(--s-3)' }}>
                {decision.band}
              </div>
              <p className="small" style={{ marginTop: 'var(--s-2)' }}>{decision.text}</p>
            </article>
          ))}
        </div>

        <p className="home__note">
          If fraud screening is unreachable, a transfer becomes a review — never an
          automatic approval. An outage turns into analyst work, not into losses.
        </p>
      </section>

      {/* ---- facts --------------------------------------------------- */}
      <section className="home__facts">
        {FACTS.map(([value, label]) => (
          <div key={label} className="home__fact">
            <div className="home__fact-value mono">{value}</div>
            <div className="label">{label}</div>
          </div>
        ))}
      </section>

      {/* ---- close --------------------------------------------------- */}
      <section className="home__close">
        <h2 className="home__headline" style={{ fontSize: 'var(--step-3)' }}>
          Have a look around.
        </h2>
        <p className="home__lede" style={{ margin: '0 auto' }}>
          Open an account and run a transfer through it, or sign in to one of the
          staff consoles with the demo credentials on the sign-in page.
        </p>
        <div className="row" style={{ justifyContent: 'center', marginTop: 'var(--s-5)' }}>
          <Link to="/register" className="btn btn--primary">Open an account</Link>
          <Link to="/login" className="btn">Sign in</Link>
        </div>
      </section>

      <footer className="home__foot">
        <span className="tiny faint">
          IND Bank — demonstration platform. Simulated rails; no real money moves.
        </span>
      </footer>
    </div>
  )
}
