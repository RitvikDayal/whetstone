import { useEffect, useState } from 'react'
import { ApiError, apiGet, readToken } from './session'
import type { Finding, FindingsResponse, RunView } from './types'

// EVERY STRING RENDERED HERE IS UNTRUSTED. Finding titles, details, grade
// reasons and skip text are model output derived from the contents of a
// repository the user did not necessarily write -- `doctor.py` records the
// same fact about `whetstone init`. React escapes text children by default and
// that is what is relied on: there is no `dangerouslySetInnerHTML` anywhere in
// this app, no markdown renderer, and a test greps for both.

function Banner({ run }: { run: RunView | null }) {
  // NO RUN AT ALL is not the same as a clean run, and rendering both as
  // silence is how a project nobody has checked reads as a project with no
  // problems. `readmodel.run_view` returns null for the first case precisely so
  // this distinction survives to a screen.
  if (run === null) {
    return (
      <div className="banner banner-quiet">
        No run has been recorded for this project. Nothing has been checked yet
        &mdash; run <code>whetstone run</code>.
      </div>
    )
  }
  return (
    <>
      {!run.finished && (
        <div className="banner banner-alarm">
          <strong>This run did not finish</strong> &mdash; status{' '}
          <code>{run.status}</code>. What follows is a partial record of a
          partial run: a finding that is absent may simply never have been
          looked for.
        </div>
      )}
      {run.skips.length > 0 && (
        <div className="banner banner-warn">
          <strong>Not everything was checked.</strong>
          <ul>
            {run.skips.map((skip, i) => (
              <li key={i}>{skip}</li>
            ))}
          </ul>
        </div>
      )}
    </>
  )
}

function Verdict({ finding }: { finding: Finding }) {
  // THE WORD, not the letter. A grade D rendered as a bare "D" is a
  // distinction a skimming reader does not make, and this exact failure --
  // a killed finding looking identical to a confirmed one -- is why
  // `readmodel` carries `killed` as a boolean rather than leaving each surface
  // to compare against a string.
  if (!finding.graded) {
    return (
      <span className="verdict verdict-none" title="This lens does not grade.">
        not graded
      </span>
    )
  }
  if (finding.killed) {
    return <span className="verdict verdict-killed">D &mdash; killed</span>
  }
  return <span className={`verdict verdict-${finding.grade}`}>{finding.grade}</span>
}

function Row({ finding }: { finding: Finding }) {
  const [open, setOpen] = useState(false)
  return (
    <li className={finding.killed ? 'finding killed' : 'finding'}>
      <button
        className="finding-head"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <Verdict finding={finding} />
        <span className={`sev sev-${finding.severity}`}>{finding.severity}</span>
        <span className="subject">{finding.subject}</span>
        <span className="title">{finding.title}</span>
      </button>
      {open && (
        <div className="finding-body">
          <p className="detail">{finding.detail}</p>
          {finding.grade_reason && (
            <p className="why">
              <strong>Why this grade:</strong> {finding.grade_reason}
            </p>
          )}
          <dl className="meta">
            <dt>id</dt>
            <dd>
              <code>{finding.short_id}</code>
            </dd>
            <dt>lens</dt>
            <dd>{finding.lens}</dd>
            <dt>rule</dt>
            <dd>{finding.rule_id}</dd>
            <dt>state</dt>
            <dd>{finding.state}</dd>
          </dl>
        </div>
      )}
    </li>
  )
}

export default function App() {
  const [token] = useState(readToken)
  const [data, setData] = useState<FindingsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!token) return
    apiGet<FindingsResponse>('api/findings', token)
      .then(setData)
      .catch((exc: unknown) =>
        setError(exc instanceof ApiError ? exc.message : String(exc)),
      )
  }, [token])

  if (!token) {
    return (
      <main>
        <h1>Whetstone</h1>
        <div className="banner banner-alarm">
          <strong>No session token.</strong> This page was opened without one,
          or the tab it was opened in has been closed. Start the control plane
          again with <code>whetstone ui</code> and use the link it prints.
        </div>
      </main>
    )
  }

  if (error) {
    return (
      <main>
        <h1>Whetstone</h1>
        <div className="banner banner-alarm">{error}</div>
      </main>
    )
  }

  if (!data) {
    return (
      <main>
        <h1>Whetstone</h1>
        <p className="muted">Reading the queue&hellip;</p>
      </main>
    )
  }

  return (
    <main>
      <h1>Whetstone</h1>
      <p className="sub">
        {data.findings.length} open finding
        {data.findings.length === 1 ? '' : 's'}
        {data.run && (
          <>
            {' '}
            &middot; tier {data.run.tier} &middot; {data.run.file_count} file
            {data.run.file_count === 1 ? '' : 's'} in scope
          </>
        )}
      </p>

      <Banner run={data.run} />

      {data.findings.length === 0 ? (
        <p className="muted">
          {data.run && data.run.skips.length > 0
            ? 'No open findings — but see above: this run did not check everything.'
            : 'No open findings.'}
        </p>
      ) : (
        <ul className="findings">
          {/* NOT re-sorted. `store/findings.py` orders by grade first and puts
              an ABSENT grade between B and C, so a measured CVE is neither
              buried under something the falsifier refuted nor ranked above a
              proven crash. Sorting by severity here because that column looks
              more important would silently invert that. */}
          {data.findings.map((f) => (
            <Row key={f.id} finding={f} />
          ))}
        </ul>
      )}

      {data.findings.some((f) => f.killed) && (
        <p className="footnote">
          Rows marked <em>killed</em> were refuted by the falsifier. They are
          shown, and sorted last, because a tool that quietly drops what it
          refuted cannot be checked.
        </p>
      )}
    </main>
  )
}
