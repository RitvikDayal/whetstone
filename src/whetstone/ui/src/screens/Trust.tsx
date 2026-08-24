import type { TrustRow } from '../types'

// THE SENTENCE IS THE FEATURE. The design's claim for earned autonomy is that
// "is this tool trustworthy here" becomes a number instead of a feeling --
// and a number without its reason is still a feeling. `autonomy.earned_level`
// returns the explanation alongside the level for exactly that reason, so a
// screen that renders the integer and drops the string has undone the feature
// while appearing to ship it.

const LEVELS = [
  'report only',
  'propose a fix',
  'draft a pull request',
  'open a pull request',
]

function describe(level: number): string {
  return LEVELS[level] ?? `level ${level}`
}

export default function Trust({ rows }: { rows: TrustRow[] }) {
  if (rows.length === 0) {
    return (
      <p className="muted">
        No lenses are declared in <code>whetstone.yaml</code>, so there is
        nothing to have earned anything. A run with no lenses examines nothing.
      </p>
    )
  }

  return (
    <>
      <p className="sub">
        What each lens has earned on this project, and why. Autonomy is earned
        by track record; config sets a ceiling that promotion cannot exceed.
      </p>

      <ul className="trust">
        {rows.map((row) => (
          <li key={row.lens} className="panel">
            <h2>
              {row.lens}
              {!row.enabled && <span className="tag">disabled</span>}
            </h2>

            <div className="levels">
              <div>
                <span className="label">acting at</span>
                <strong>
                  {row.earned_level} &mdash; {describe(row.earned_level)}
                </strong>
              </div>
              <div>
                <span className="label">ceiling</span>
                <strong>
                  {row.configured_ceiling} &mdash;{' '}
                  {describe(row.configured_ceiling)}
                </strong>
              </div>
              <div>
                <span className="label">acceptance</span>
                <strong>
                  {/* null, NOT 0. `decisions.py` refuses to hand back a bare
                      float when there are no decisions, because 0.0 is the
                      claim that everything was rejected -- the opposite claim.
                      And the rate never travels without its sample size: a
                      rate computed from two decisions is not a rate. */}
                  {row.acceptance_rate === null
                    ? 'no decisions yet'
                    : `${Math.round(row.acceptance_rate * 100)}% of ${row.sample}`}
                </strong>
              </div>
            </div>

            <p className="why">{row.reason}</p>
          </li>
        ))}
      </ul>

      <p className="footnote">
        Levels above 1 are computed and shown here, and a lens only acts on one
        where a writer exists for it to authorise.
      </p>
    </>
  )
}
