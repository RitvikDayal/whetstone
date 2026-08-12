"""A self-contained HTML report.

No external requests: no CDN scripts, no web fonts, no remote images. The file
must render identically on a machine with no network.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, select_autoescape

from ..errors import ReportError
from ..runner import RunResult
from ..store.findings import Finding

_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Whetstone — {{ project_name }}</title>
<style>
  :root {
    --bg: #ffffff; --fg: #17181c; --muted: #61646b;
    --line: #e3e5e9; --card: #f7f8fa;
    --low: #6b7280; --medium: #b45309; --high: #b91c1c; --critical: #7f1d1d;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14161a; --fg: #e8eaee; --muted: #9aa0a8;
      --line: #2a2e35; --card: #1b1e23;
      --low: #9aa0a8; --medium: #f0b429; --high: #f87171; --critical: #fca5a5;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2rem 1.25rem; background: var(--bg); color: var(--fg);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif;
  }
  main { max-width: 60rem; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
  .sub { color: var(--muted); margin: 0 0 1.75rem; }
  .notice {
    border: 1px solid var(--line); border-left: 3px solid var(--medium);
    background: var(--card); padding: .85rem 1rem; border-radius: 6px;
    margin-bottom: 1.5rem;
  }
  .notice h2 { font-size: .95rem; margin: 0 0 .4rem; }
  .notice ul { margin: 0; padding-left: 1.1rem; color: var(--muted); }
  /* The run-did-not-finish banner. Deliberately louder than .notice: a skip
     list says part of the work was not done, this says the whole document may
     be a partial record of a partial run. */
  .alarm {
    border: 1px solid var(--high); border-left: 5px solid var(--high);
    background: var(--card); padding: .9rem 1rem; border-radius: 6px;
    margin-bottom: 1.5rem;
  }
  .alarm h2 { font-size: 1rem; margin: 0 0 .4rem; color: var(--high); }
  .alarm p { margin: 0; }
  .finding {
    border: 1px solid var(--line); border-radius: 8px; background: var(--card);
    padding: 1rem 1.1rem; margin-bottom: .85rem;
  }
  .finding h3 { font-size: 1rem; margin: 0 0 .35rem; }
  .meta { color: var(--muted); font-size: .82rem; margin-bottom: .5rem; }
  .detail { white-space: pre-wrap; margin: 0; }
  /* pre-wrap honours newlines and collapses nothing, and on its own it will
     not break a token that has no space in it. Real advisory text carries
     reference URLs and package specifiers, and neither has a break
     opportunity.

     Measured in Chrome at a 390px viewport, before and after this rule. A
     PYSEC advisory description ending in a GitHub Security Advisory URL:
     documentScrollWidth 429 against clientWidth 390, so 39px of sideways
     scroll on a phone -- 0px after. A single 200,000-character token:
     scrollWidth 1,935,097px -- 390px after, with the text still present
     rather than clipped.

     `anywhere` rather than `break-word`: both break mid-token as a last
     resort, but only `anywhere` also stops the oversized token from setting
     the element's min-content width, and that contribution is what dragged
     the whole document wider than the viewport. */
  .detail, .meta, .finding h3, .notice li, .alarm p {
    overflow-wrap: anywhere;
  }
  .sev { font-weight: 600; text-transform: uppercase; font-size: .72rem;
         letter-spacing: .04em; }
  .sev-low { color: var(--low); } .sev-medium { color: var(--medium); }
  .sev-high { color: var(--high); } .sev-critical { color: var(--critical); }
  .empty { color: var(--muted); }
</style>
</head>
<body>
<main>
  <h1>Whetstone — {{ project_name }}</h1>
  <p class="sub">
    {{ findings|length }} open finding{{ '' if findings|length == 1 else 's' }}
    {%- if run %} · tier {{ run.tier }}
    · {{ run.file_count }} file{{ '' if run.file_count == 1 else 's' }} in scope
    · {{ run.new }} new, {{ run.seen }} already known
    {%- if not run.finished %} · run {{ run.status }}{% endif %}{% endif %}
  </p>

  {% if run is none %}
  <div class="alarm">
    <h2>No run has been recorded</h2>
    <p>Nothing has ever run against this project's state, so this document
    reports on nothing at all. It is not evidence that the project is clean.
    Run <code>whetstone run</code> first.</p>
  </div>
  {% elif not run.finished %}
  <div class="alarm">
    <h2>This run did not finish — status “{{ run.status }}”</h2>
    <p>The most recent run ended before it was done: it was interrupted, or it
    failed partway through. Everything below is a partial record of a partial
    run, and a finding that is absent here may simply never have been looked
    for. Re-run <code>whetstone run</code> before trusting this.</p>
  </div>
  {% endif %}

  {% if run and run.skips %}
  <div class="notice">
    <h2>Not everything was checked</h2>
    <ul>{% for skip in run.skips %}<li>{{ skip }}</li>{% endfor %}</ul>
  </div>
  {% endif %}

  {% if not findings %}
    {% if run and run.finished %}
    <p class="empty">No open findings.</p>
    {% else %}
    <p class="empty">No open findings were recorded — which, for the reason
    above, says nothing about whether there are any.</p>
    {% endif %}
  {% endif %}

  {% for f in findings %}
  <article class="finding">
    <h3>{{ f.title }}</h3>
    <p class="meta">
      <span class="sev sev-{{ f.severity }}">{{ f.severity }}</span>
      · {{ f.lens }} · {{ f.rule_id }} · {{ f.subject }}
      · first seen {{ f.first_seen_run }}
    </p>
    <p class="detail">{{ f.detail }}</p>
  </article>
  {% endfor %}
</main>
</body>
</html>
"""


def render_report(
    findings: list[Finding], *, project_name: str, run: RunResult | None
) -> str:
    env = Environment(autoescape=select_autoescape(default_for_string=True))
    return env.from_string(_TEMPLATE).render(
        findings=findings, project_name=project_name, run=run
    )


# Windows resolves these names to character devices no matter which directory
# they appear in, and an extension does not disarm them: `NUL`, `NUL.html` and
# `nul.report.html` are all the null device. Writing there succeeds, discards
# every byte, and leaves nothing on disk -- measured: `--out NUL` printed
# "Wrote ...\\NUL" and exited 0 with no file created.
#
# Refused on every platform, not just Windows. On POSIX `NUL` is an ordinary
# filename, so this rejects something the OS would allow; the trade is
# deliberate and cheap. Nobody names a report `CON.html`, a whetstone.yaml is
# committed and read on other people's machines, and the alternative is a
# guard that exists only where a test would have to skip to reach it.
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in range(1, 10)}
    | {f"lpt{digit}" for digit in range(1, 10)}
)


def _reserved_device_name(path: Path) -> str | None:
    """The reserved name *path* resolves to as a device, or None."""
    # Windows strips trailing dots and spaces before resolving, and treats
    # everything from the first dot onward as an extension, so the stem of
    # `nul.report.html ` is still `nul`.
    stem = path.name.strip(" .").split(".")[0].lower()
    return stem if stem in _RESERVED_DEVICE_NAMES else None


def _link_count(path: Path) -> int:
    """How many names *path* has, or 1 when that cannot be established.

    `st_nlink` is populated on Windows as well as POSIX (Python fills it from
    GetFileInformationByHandle), so this is one check rather than a platform
    branch. A path that does not exist yet, or that cannot be stat'ed, has no
    aliasing to hide and reports 1; the write itself reports any real problem.
    """
    try:
        return path.stat().st_nlink
    except OSError:
        return 1


def write_report(path: Path, html: str) -> Path:
    """Write *html* to *path*, refusing a symlink, a directory, or a device.

    `--out` is user-supplied, the same shape of problem
    `initialize/wizard.py::_refuse_symlinked_target` already solved for
    `whetstone.yaml`: a symlink at *path* decides where the bytes land, and a
    link committed to (or dropped into) a project decides that on the
    project's behalf rather than the caller's. Refused unconditionally, not
    just when it would escape somewhere -- same stance the wizard takes.

    A HARDLINK is refused too, and it is not the same check. A hardlink is not
    a symlink, so `is_symlink()` returns False, and it is not a redirection
    either -- there is no target to follow, so `resolve()` in
    `cli._report_target` sees an ordinary path inside the project and both
    layers pass. Confirmed writing outside the project root through one. The
    signal that remains is the link count, which is >1 for exactly the file
    that has another name somewhere else.

    WHAT THIS DOES NOT CLOSE, stated rather than implied: the check and the
    write are separate syscalls on a path, with no `O_NOFOLLOW` and no held
    descriptor between them, so a link created in that window is not seen by
    either layer. Closing that needs an fd-based write, and `O_NOFOLLOW` does
    not exist on Windows, so it is not a portable M0 fix -- filed rather than
    faked. The practical bound on all of this is that git cannot carry a
    hardlink: reaching this state means something on the machine already made
    one.

    Raises ReportError rather than letting write_text's OSError (a directory,
    a missing parent, a permission failure) reach the CLI as a bare traceback.
    """
    device = _reserved_device_name(path)
    if device is not None:
        raise ReportError(
            f"{path} names {device.upper()}, a reserved device on Windows. "
            "Writing there succeeds and discards every byte, so the report "
            "would be reported as written and would not exist. Pick another "
            "--out path."
        )
    try:
        is_link = path.is_symlink()
    except OSError:  # pragma: no cover - unreadable parent; the write reports it
        is_link = False
    if is_link:
        raise ReportError(
            f"{path} is a symlink. Whetstone will not write the report through "
            "one. Remove or replace it with a real path."
        )
    if path.is_dir():
        raise ReportError(
            f"{path} is a directory. Point --out at a file path, not a directory."
        )
    if _link_count(path) > 1:
        raise ReportError(
            f"{path} has more than one name on this filesystem (it is a "
            "hardlink). Writing it would write through to whatever else points "
            "at the same file, including somewhere outside the project. Remove "
            "it, or point --out at a fresh path."
        )
    try:
        path.write_text(html, encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"could not write the report to {path}: {exc}") from exc
    return path
