#!/usr/bin/env bash
# Apply server-side branch protection to main.
#
# GitHub refuses rulesets on a PRIVATE repo on the Free plan:
#   403 "Upgrade to GitHub Pro or make this repository public to enable this
#        feature."
#
# So this cannot be applied yet. Run it the moment either becomes true:
#   - the repo is made public, or
#   - the account moves to GitHub Pro
#
# Until then, .githooks/pre-push is the enforcement, and it is client-side
# only. Enable it with:  git config core.hooksPath .githooks
set -euo pipefail

REPO="${1:-RitvikDayal/whetstone}"

echo "Applying branch protection to main on ${REPO}…"

gh api -X POST "repos/${REPO}/rulesets" --input - <<'JSON'
{
  "name": "protect-main",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/main"], "exclude": [] } },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": true,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true,
        "allowed_merge_methods": ["squash"]
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "test (ubuntu-latest, 3.11)" },
          { "context": "test (ubuntu-latest, 3.12)" },
          { "context": "test (windows-latest, 3.11)" },
          { "context": "test (windows-latest, 3.12)" }
        ]
      }
    }
  ]
}
JSON

echo "Done. Verify with: gh api repos/${REPO}/rulesets"
