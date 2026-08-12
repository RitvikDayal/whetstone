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
  .finding {
    border: 1px solid var(--line); border-radius: 8px; background: var(--card);
    padding: 1rem 1.1rem; margin-bottom: .85rem;
  }
  .finding h3 { font-size: 1rem; margin: 0 0 .35rem; }
  .meta { color: var(--muted); font-size: .82rem; margin-bottom: .5rem; }
  .detail { white-space: pre-wrap; margin: 0; }
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
    {%- if run %} · tier {{ run.tier }} · {{ run.file_count }} files in scope
    · {{ run.new }} new, {{ run.seen }} already known{% endif %}
  </p>

  {% if run and run.skips %}
  <div class="notice">
    <h2>Not everything was checked</h2>
    <ul>{% for skip in run.skips %}<li>{{ skip }}</li>{% endfor %}</ul>
  </div>
  {% endif %}

  {% if not findings %}
    <p class="empty">No open findings.</p>
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


def write_report(path: Path, html: str) -> Path:
    """Write *html* to *path*, refusing a symlink or a directory target.

    `--out` is user-supplied, the same shape of problem
    `initialize/wizard.py::_refuse_symlinked_target` already solved for
    `whetstone.yaml`: a symlink at *path* decides where the bytes land, and a
    link committed to (or dropped into) a project decides that on the
    project's behalf rather than the caller's. Refused unconditionally, not
    just when it would escape somewhere -- same stance the wizard takes.

    Raises ReportError rather than letting write_text's OSError (a directory,
    a missing parent, a permission failure) reach the CLI as a bare traceback.
    """
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
    try:
        path.write_text(html, encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"could not write the report to {path}: {exc}") from exc
    return path
