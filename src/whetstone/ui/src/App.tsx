import { useCallback, useEffect, useState } from 'react'
import Cost from './screens/Cost'
import Findings from './screens/Findings'
import Run from './screens/Run'
import Trust from './screens/Trust'
import { ApiError, apiGet, readToken } from './session'
import type { CostView, FindingsResponse, TrustRow } from './types'

const TABS = ['findings', 'run', 'trust', 'cost'] as const
type Tab = (typeof TABS)[number]

export default function App() {
  const [token] = useState(readToken)
  const [tab, setTab] = useState<Tab>('findings')
  const [queue, setQueue] = useState<FindingsResponse | null>(null)
  const [trust, setTrust] = useState<TrustRow[] | null>(null)
  const [cost, setCost] = useState<CostView | null>(null)
  const [error, setError] = useState<string | null>(null)

  // ONE REFRESH FOR EVERY SURFACE, called after any mutation. A decision
  // changes the queue AND the acceptance rate the trust screen reads, and
  // updating one of them locally while leaving the other stale is how two views
  // of one store come to disagree inside a single page.
  const refresh = useCallback(() => {
    if (!token) return
    setError(null)
    Promise.all([
      apiGet<FindingsResponse>('api/findings', token),
      apiGet<TrustRow[]>('api/trust', token),
      apiGet<CostView>('api/costs', token),
    ])
      .then(([findings, trustRows, costView]) => {
        setQueue(findings)
        setTrust(trustRows)
        setCost(costView)
      })
      .catch((exc: unknown) =>
        setError(exc instanceof ApiError ? exc.message : String(exc)),
      )
  }, [token])

  useEffect(refresh, [refresh])

  if (!token) {
    return (
      <main>
        <h1>Whetstone</h1>
        <div className="banner banner-alarm">
          <strong>No session token.</strong> This page was opened without one,
          or in a tab that never had one. Start the control plane again with{' '}
          <code>whetstone ui</code> and use the link it opens &mdash; the token
          is different every time, and it is deliberately not printed.
        </div>
      </main>
    )
  }

  return (
    <main>
      <header>
        <h1>Whetstone</h1>
        <nav>
          {TABS.map((name) => (
            <button
              key={name}
              className={name === tab ? 'tab active' : 'tab'}
              aria-current={name === tab}
              onClick={() => setTab(name)}
            >
              {name}
              {name === 'findings' && queue && queue.findings.length > 0 && (
                <span className="count">{queue.findings.length}</span>
              )}
            </button>
          ))}
        </nav>
      </header>

      {error && <div className="banner banner-alarm">{error}</div>}

      {tab === 'findings' &&
        (queue ? (
          <Findings
            findings={queue.findings}
            run={queue.run}
            token={token}
            onDecided={refresh}
          />
        ) : (
          <p className="muted">Reading the queue&hellip;</p>
        ))}

      {tab === 'run' && <Run token={token} onFinished={refresh} />}

      {tab === 'trust' &&
        (trust ? <Trust rows={trust} /> : <p className="muted">Reading&hellip;</p>)}

      {tab === 'cost' &&
        (cost ? <Cost view={cost} /> : <p className="muted">Reading&hellip;</p>)}
    </main>
  )
}
