#!/usr/bin/env bash
# Apply server-side branch protection to main.
#
# Runnable now: the repository is public, and GitHub only refuses rulesets on a
# PRIVATE repo on the Free plan.
#
# WHAT THIS DOES NOT REQUIRE, AND WHY. An earlier version of this script asked
# for `required_approving_review_count: 1` with `require_code_owner_review`, and
# CODEOWNERS names @coderabbitai -- a bot. That combination is one of two
# things on a repository with one human: a deadlock, if the bot's review does
# not satisfy code-owner approval, or self-approval theatre if the maintainer
# approves their own pull request. Neither is a second pair of eyes.
#
# So the rules below are the ones that actually bite with a single maintainer:
# changes go through a pull request, every status check passes, and every review
# thread is resolved before merge. That last one is real -- CodeRabbit opens
# threads and they have to be answered, not waved through.
#
# The honest version of "a second reviewer" is a second maintainer. Until there
# is one, this claims less than it could and means all of it. The same argument
# is recorded in release.yml about `prevent_self_review`.
#
# .githooks/pre-push remains as a client-side pre-check. Enable it with:
#   git config core.hooksPath .githooks
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
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
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
          { "context": "test (windows-latest, 3.12)" },
          { "context": "wheel" }
        ]
      }
    }
  ]
}
JSON

echo "Done. Verify with: gh api repos/${REPO}/rulesets"
