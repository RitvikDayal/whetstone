// The shapes `whetstone/readmodel.py` returns. Kept deliberately close to that
// module's field names -- a rename on either side should break the build here
// rather than render an `undefined` that looks like an absent value.

export interface Finding {
  id: string
  short_id: string
  lens: string
  rule_id: string
  subject: string
  title: string
  detail: string
  severity: string
  state: string
  grade: string | null
  grade_reason: string | null
  /** grade === "D". A verdict the falsifier reached. */
  killed: boolean
  /** Whether the lens graded it AT ALL. `hygiene` never does, and an ungraded
   *  finding is not a refuted one -- rendering the second as the first reports
   *  a measured CVE as dismissed by a stage that never looked at it. */
  graded: boolean
  first_seen_run: string
  last_seen_run: string
}

export interface RunView {
  run_id: string
  tier: string
  file_count: number
  status: string
  finished: boolean
  new: number
  seen: number
  skips: string[]
  lens_count: number | null
}

export interface FindingsResponse {
  findings: Finding[]
  /** null means NO RUN HAS EVER HAPPENED here, which is a different thing from
   *  a run that found nothing. The first means nothing was checked. */
  run: RunView | null
}
