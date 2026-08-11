"""Flag dependencies with known advisories, via pip-audit.

Node support lands with M1. This detector deliberately reports *nothing* rather
than guessing when pip-audit is unavailable -- and says so.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

from ...base import Candidate, Evidence, EvidenceKind, RunContext, Severity

_MANIFESTS = ("pyproject.toml", "requirements.txt", "setup.cfg")
_TIMEOUT_SECONDS = 120


def _run_pip_audit(project_root: Path) -> str:
    """Run pip-audit and return raw JSON. Raises FileNotFoundError if absent."""
    proc = subprocess.run(
        ["pip-audit", "--format", "json", "--progress-spinner", "off"],
        cwd=project_root,
        capture_output=True,
        encoding="utf-8",
        timeout=_TIMEOUT_SECONDS,
    )
    # pip-audit exits 1 when it finds vulnerabilities -- that is success for us.
    if proc.returncode not in (0, 1):
        raise RuntimeError(proc.stderr.strip() or f"exit {proc.returncode}")
    return proc.stdout


class DepsDetector:
    id = "deps"

    def detect(self, ctx: RunContext) -> Iterator[Candidate]:
        if not any((ctx.project_root / name).is_file() for name in _MANIFESTS):
            ctx.skip(
                "hygiene/deps: no Python manifest found "
                f"({', '.join(_MANIFESTS)}); nothing to audit."
            )
            return

        try:
            raw = _run_pip_audit(ctx.project_root)
        except FileNotFoundError:
            ctx.skip(
                "hygiene/deps: pip-audit is not installed, so dependency "
                "advisories were NOT checked. Install it with "
                "`uv tool install pip-audit`."
            )
            return
        except (subprocess.TimeoutExpired, RuntimeError) as exc:
            ctx.skip(f"hygiene/deps: pip-audit failed ({exc}); advisories not checked.")
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            ctx.skip(f"hygiene/deps: pip-audit returned unparseable JSON ({exc}).")
            return

        for dependency in payload.get("dependencies", []):
            name = dependency.get("name", "unknown")
            version = dependency.get("version", "unknown")
            for vuln in dependency.get("vulns", []):
                fixes = vuln.get("fix_versions") or []
                fix_text = (
                    f"Fixed in {', '.join(fixes)}."
                    if fixes
                    else "No fixed version is published yet."
                )
                yield Candidate(
                    lens="hygiene",
                    rule_id=vuln.get("id", "unknown-advisory"),
                    subject=name,
                    title=f"{name} {version} has advisory {vuln.get('id', '?')}",
                    detail=(
                        f"{vuln.get('description', 'No description provided.')}\n"
                        f"{fix_text}"
                    ),
                    severity=Severity.high,
                    evidence=Evidence(
                        kind=EvidenceKind.metric,
                        summary=f"pip-audit advisory {vuln.get('id', '?')}",
                        data={
                            "package": name,
                            "installed": version,
                            "advisory": vuln.get("id"),
                            "fix_versions": fixes,
                        },
                    ),
                )
