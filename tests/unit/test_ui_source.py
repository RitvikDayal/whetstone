"""Static scans over the front-end, in the style the Python side already uses.

There is no JavaScript test runner in this project and adding one is not
justified by one screen. What IS justified is the same kind of source scan
`test_invariants.py` runs over `src/` -- cheap, mechanical, and aimed at the
handful of spellings that would undo a control the server pays for.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "src" / "whetstone" / "ui"

# RECURSIVE, under `ui/src` only. The first version globbed `src/*.ts` and
# `src/*.tsx` -- one level -- and was written when every module sat directly in
# `src/`. The moment the app grew a `src/screens/` directory, four files
# rendering untrusted model output stopped being scanned and every test here
# stayed green. That is the defect this file exists to catch, committed by this
# file.
#
# `ui/node_modules` is excluded rather than left to a glob: a bare `rglob`
# under `ui/` pulls in thirty TypeScript files from `@babel` and `@jridgewell`,
# and a scan that reports on somebody else's dependencies is a scan people
# learn to ignore.
SOURCES = sorted(
    path
    for pattern in ("**/*.ts", "**/*.tsx")
    for path in (UI / "src").rglob(pattern)
    if "node_modules" not in path.parts
)


def _code_only(source: str) -> str:
    """*source* with comments removed, so the scan reads code and not prose.

    The Python side hit this first: `test_cli.py` strips comments with
    `tokenize` before scanning, because a scan that cannot tell code from a
    comment ABOUT the code punishes writing the comment. This file's own
    docstrings name every banned spelling.

    Deliberately simple: whole-line `//` comments and `/* ... */` blocks. A
    banned spelling in a TRAILING comment on a line of code would still fail,
    which is a rule worth keeping -- put the explanation on its own line.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    kept = [
        line
        for line in without_blocks.splitlines()
        if not line.strip().startswith("//")
    ]
    return "\n".join(kept)


def test_the_scan_is_reaching_the_front_end():
    """Without this, a moved or renamed directory makes every test below pass
    by matching nothing -- which is how a static scan goes quietly vacuous."""
    assert len(SOURCES) >= 4, [p.name for p in SOURCES]


def test_the_scan_reaches_every_module_the_bundle_imports():
    """Not a count -- the actual set, compared against what is on disk.

    A count is satisfied by any four files, so it stayed green when a new
    subdirectory of four MORE files went unscanned. This asserts nothing under
    `ui/src` is missing from `SOURCES`, so adding a directory either extends the
    scan or fails here.
    """
    on_disk = {
        path.relative_to(UI).as_posix()
        for path in (UI / "src").rglob("*")
        if path.suffix in (".ts", ".tsx") and "node_modules" not in path.parts
    }
    scanned = {path.relative_to(UI).as_posix() for path in SOURCES}
    assert on_disk == scanned, on_disk ^ scanned


def test_the_scan_does_not_wander_into_dependencies():
    """A scan that reports on `@babel`'s source is one people stop reading."""
    assert not any("node_modules" in path.parts for path in SOURCES)


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_nothing_renders_untrusted_content_as_html(path: Path):
    """EVERY STRING THIS APP RENDERS IS UNTRUSTED.

    Finding titles, details, grade reasons and skip text are model output
    derived from the contents of a repository the user did not necessarily
    write -- `doctor.py` records the same fact about `whetstone init`. React
    escapes text children by default, and that default is the whole defence.

    The page also holds the session token, and the token reaches every route,
    so script execution here is not an information leak. The server sends a CSP
    with no `'unsafe-inline'` as the second control; this is the first.
    """
    source = _code_only(path.read_text(encoding="utf-8"))
    for spelling in ("dangerouslySetInnerHTML", "innerHTML", "outerHTML",
                     "document.write", "eval("):
        assert spelling not in source, f"{path.name} uses {spelling}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_nothing_reaches_off_the_origin(path: Path):
    """Same rule `report/html.py` holds itself to: no CDN, no remote fonts, no
    remote images. The control plane must work on a machine with no network."""
    source = _code_only(path.read_text(encoding="utf-8"))
    assert not re.search(r"https?://(?!127\.0\.0\.1|localhost)", source), path.name


def test_the_token_is_never_put_in_a_url():
    """A query-string token lands in access logs and `Referer` headers. The
    fragment is the only place it may travel, and `session.ts` reads it from
    there exactly once."""
    session = (UI / "src" / "session.ts").read_text(encoding="utf-8")
    assert "location.hash" in session
    assert "searchParams.set" not in session
    assert "?t=" not in session


def test_the_token_is_sent_as_a_custom_header():
    """The CSRF control, and the only one. A cross-origin form cannot set a
    custom header; a cross-origin fetch that tries triggers a preflight the
    server answers with no CORS headers at all."""
    session = (UI / "src" / "session.ts").read_text(encoding="utf-8")
    assert "'X-Whetstone-Token'" in session
    assert "credentials: 'omit'" in session, (
        "cookies must be explicitly off -- a cookie is ambient authority and "
        "would undo the reason the token lives in a header"
    )


def test_the_bundle_is_built_without_an_inline_module_preload_polyfill():
    """Vite injects that polyfill as an INLINE script, and the server's CSP
    carries no `'unsafe-inline'`. Leaving it on means either a blank page or a
    CSP relaxed on the one page that renders model-authored strings."""
    # Through `_code_only` like every other scan here: a comment mentioning
    # the setting would otherwise satisfy an assertion about the setting.
    config = _code_only((UI / "vite.config.ts").read_text(encoding="utf-8"))
    assert "modulePreload" in config
    assert "polyfill: false" in config


def test_the_front_end_declares_no_runtime_dependency_beyond_react():
    """A dependency added here ships inside the wheel and runs in a page
    holding the session token. React and its DOM renderer are the budget."""
    manifest = json.loads((UI / "package.json").read_text(encoding="utf-8"))
    assert set(manifest["dependencies"]) == {"react", "react-dom"}


def test_the_built_bundle_has_no_inline_script(request):
    """The measurement, not the configuration. `vite.config.ts` asking for no
    polyfill and the emitted HTML having none are different claims."""
    index = UI / "dist" / "index.html"
    if not index.is_file():
        pytest.skip("the bundle is not built; the wheel test in test_package.py "
                    "builds one and is the gate")
    del request
    html = index.read_text(encoding="utf-8")
    for match in re.finditer(r"<script\b([^>]*)>", html):
        assert "src=" in match.group(1), f"inline <script> in the shell: {match.group(0)}"
