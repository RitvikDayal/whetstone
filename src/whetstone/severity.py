"""Severity vocabulary, shared by config and lenses.

This lives here rather than in `lenses/base.py` so that both sides can depend on
it without one depending on the other. Config needs it to validate
`severity_floor`; lenses need it to score candidates. Importing `lenses` from
`config` would point a core module at the plugin layer, which is backwards, and
would put a cycle one careless import away.

`lenses.base` re-exports both names, so the existing import site still works.
"""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


_SEVERITY_ORDER: dict[str, int] = {
    Severity.low: 0,
    Severity.medium: 1,
    Severity.high: 2,
    Severity.critical: 3,
}


def severity_at_least(value: Severity, floor: Severity) -> bool:
    """True when *value* is at or above *floor*."""
    return _SEVERITY_ORDER[value] >= _SEVERITY_ORDER[floor]
