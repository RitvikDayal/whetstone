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

export interface TrustRow {
  lens: string
  enabled: boolean
  configured_ceiling: number
  earned_level: number
  /** The SENTENCE `autonomy.earned_level` returns. Not decoration: the design's
   *  claim is that "is this tool trustworthy here" becomes a number instead of a
   *  feeling, and a number without its reason is still a feeling. */
  reason: string
  /** null, not 0, when no decisions have been recorded. 0.0 is the claim that
   *  everything was rejected, which is the opposite claim. */
  acceptance_rate: number | null
  sample: number
}

export interface StageCost {
  stage: string
  subject: string
  cost_usd: number | null
  tokens: number
  wall_seconds: number
  source: string
}

export interface CostRecord {
  run_id: string
  lens: string
  tier: string
  ceiling_usd: number | null
  spent_usd: number
  tokens: number
  calls: number
  unmeasured_calls: number
  stages: StageCost[]
}

export interface CostView {
  records: CostRecord[]
  total_usd: number
  total_calls: number
  /** Calls whose cost the provider could not measure. A ceiling enforced
   *  against a total known to be short is the single most important thing a
   *  cost screen can say. */
  unmeasured_calls: number
  /** Files that could not be read, or fields that were not numbers. Spend that
   *  happened and cannot be shown -- never silently dropped. */
  unreadable: string[]
  lenses_with_records: string[]
}

export interface ConfigView {
  project: string
  tier: string
  usd_per_run: number | null
  calls_per_day: number | null
  lenses: string[]
  /** What the ceiling above does NOT bound. Rendered next to the number, not
   *  in a footnote: a ceiling shown without these is a bound the user believes
   *  they have. */
  caveats: string[]
  project_root: string
}

export type RunEvent =
  | {
      kind: 'run_started'
      run_id: string
      tier: string
      file_count: number
      lens_count: number
      lenses: string[]
      skips: string[]
    }
  | { kind: 'lens_started'; run_id: string; lens: string }
  | {
      kind: 'lens_finished'
      run_id: string
      lens: string
      new: number
      seen: number
      skips: string[]
    }
  | {
      kind: 'run_finished'
      run_id: string
      status: string
      new: number
      seen: number
      skips: string[]
    }
  | { kind: 'error'; error: string }

export const DISPOSITIONS = [
  'verify',
  'implement',
  'hand_off',
  'needs_evidence',
  'defer',
  'reject',
] as const

export type Disposition = (typeof DISPOSITIONS)[number]
