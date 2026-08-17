"""Where a finding goes once a human has decided about it.

NO MODEL IS ON THE PUBLICATION PATH, EVER. The design says so and it is not a
performance concern: a sink writes into somebody else's issue tracker under
their credentials, so the text it publishes must be the text that was decided
about, not a rewriting of it.

NOTHING HERE MERGES. `github-pr` opens a pull request -- draft or ready
depending on the earned level -- and that is the end of what Whetstone does.
`tests/unit/test_invariants.py` scans this package with everything else and
fails on a merge call, so the absence is asserted rather than asserted-in-prose.
"""

from __future__ import annotations

from .base import Publication, Sink, SinkError

__all__ = ["Publication", "Sink", "SinkError"]
