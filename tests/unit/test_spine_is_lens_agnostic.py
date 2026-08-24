"""The M2 abstraction gate, written BEFORE the lens it measures.

THE DESIGN CALLS M2 THE REAL DECISION POINT: "If two lens packs with genuinely
different evidence types do not fit the spine cleanly, the abstraction is wrong.
Fixing it there is cheap; fixing it after a plugin API is public is not."

That needs a mechanical answer rather than a judgement made afterwards, and it
needs one that exists BEFORE the browser code -- a gate written later is a gate
fitted to whatever was built. This file is written against a tree with no
`rendered-ui` lens in it, and it passes. From here it fails the moment the second
lens leaks into the spine.

IT MEASURES COUPLING, NOT SPELLING, and the first version did not. Scanning raw
text for browser vocabulary flagged five things on a clean tree: the word
"screenshot" in a sentence about terminal output, a comment in `doctor.py`
describing what M2 will do, `EvidenceKind.capture`'s docstring, and the
`<meta name="viewport">` tag in the HTML report -- which is the report's own
responsive design and has nothing to do with any lens. A gate that fires on
prose gets an allowlist per false positive, and an allowlist nobody reads is the
exemption mechanism failing open.

So it walks the AST and looks at IDENTIFIERS: imports, attribute names, field
names, function names, and comparisons against a lens name. A comment planning
future work is documentation. A field called `viewport` on `RunContext` is
coupling.

WHAT A SPINE CHANGE DURING M2 MEANS. Not automatically a failure -- it may be a
genuine gap any third lens would hit too. What is forbidden is an UNARGUED one:
`_ARGUED_SPINE_KNOWLEDGE` takes a reason, and the reason must say why a lens
other than `rendered-ui` would need the same thing.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "whetstone"

# The lens's own package. Everything inside it may know whatever it likes.
_THE_LENS = "lenses/rendered_ui"

# Identifier fragments only a browser-driving lens should need. Checked against
# names rather than text, so `# M2's browser lens will...` is documentation and
# `browser = launch()` is coupling.
_BROWSER_IDENTIFIERS = (
    "playwright",
    "chromium",
    "webkit",
    "screenshot",
    "viewport",
    "bounding_box",
    "boundingbox",
    "devtools",
    "browser",
)

# (module, identifier): why the SPINE legitimately carries this. The reason must
# argue that a lens other than rendered-ui would need it too -- that is the
# difference between a gap in the abstraction and an accommodation for one lens.
_ARGUED_SPINE_KNOWLEDGE: dict[tuple[str, str], str] = {
    ("config/model.py", "viewports"):
        "the design's own config surface (section 3.1): `environment.app` "
        "declares the app's URL, routes and viewports because ANY lens that "
        "needs the application RUNNING needs them -- rendered-ui and product-ux "
        "both do, and a third lens driving the app would too. Config is "
        "declarative and validated centrally; pushing it into lens_options "
        "would mean a typo in a viewport is accepted silently.",
    ("server/serve.py", "webbrowser"):
        "the STDLIB module that asks the operating system to open a URL. This "
        "is a word collision with the lens, not knowledge of it: `webbrowser."
        "open` hands a string to the user's default application and cannot "
        "drive, inspect, screenshot or measure anything. The control plane "
        "is not a lens and does not go through the lens contract at all -- it is "
        "a surface over the store, in the same category as `cli.py` and "
        "`report/html.py`. Renaming the import to dodge this scan was the "
        "alternative and is worse: a guard you hide from is a guard that stops "
        "working while still reporting green.",
    ("server/serve.py", "open_browser"):
        "the flag behind `whetstone ui --no-open`, naming the same stdlib call "
        "as the entry above. The control plane is not a lens.",
    ("cli.py", "open_browser"):
        "the CLI passing that flag through -- same stdlib call, one frame up, "
        "and the command surface is not a lens either.",
}


def _spine_modules() -> list[Path]:
    return sorted(
        p
        for p in _SRC.rglob("*.py")
        if _THE_LENS not in p.relative_to(_SRC).as_posix()
    )


def _identifiers(tree: ast.AST) -> set[str]:
    """Every name the module DEFINES or USES, excluding strings and comments."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
            for alias in node.names:
                found.add(alias.name)
    return found


def _leaks_in(path: Path) -> list[str]:
    suffix = path.relative_to(_SRC).as_posix()
    names = _identifiers(ast.parse(path.read_text(encoding="utf-8")))
    out = []
    for name in sorted(names):
        lowered = name.lower()
        for fragment in _BROWSER_IDENTIFIERS:
            if fragment in lowered and (suffix, name) not in _ARGUED_SPINE_KNOWLEDGE:
                out.append(f"{suffix}: {name}")
                break
    return out


def test_the_spine_does_not_know_about_browsers():
    """The gate. Passes on a tree with no rendered-ui lens; fails on a leak."""
    modules = _spine_modules()
    assert len(modules) >= 20, f"the scan is not reaching src/: {len(modules)}"

    leaks = [leak for path in modules for leak in _leaks_in(path)]
    assert leaks == [], (
        f"the spine has learned about browsers: {leaks}. Either move it into "
        f"{_THE_LENS}/, or add (module, identifier) to _ARGUED_SPINE_KNOWLEDGE "
        "with a reason explaining why a THIRD lens would need the same thing. "
        "An accommodation for one lens is the abstraction failing, which is "
        "what M2 exists to detect."
    )


def test_the_gate_can_see_a_leak(tmp_path):
    """The counterweight, on real source rather than on the regex.

    A scan that matches nothing makes the gate above vacuous, and `leaks == []`
    looks identical either way. Each planted line is a way the second lens would
    actually leak.
    """
    for planted in (
        "from playwright.sync_api import sync_playwright\n",
        "class RunContext:\n    viewport: tuple\n",
        "def take_screenshot(page):\n    pass\n",
        "box = element.bounding_box()\n",
        "browser = launch()\n",
    ):
        module = tmp_path / "leak.py"
        module.write_text(planted, encoding="utf-8")
        names = _identifiers(ast.parse(planted))
        assert any(
            frag in n.lower() for n in names for frag in _BROWSER_IDENTIFIERS
        ), planted


def test_the_gate_does_not_fire_on_prose_or_on_the_reports_own_html():
    """The false positives the first version produced on a clean tree.

    All five were real modules and none was coupling: the word "screenshot" in a
    sentence about terminal output, a comment describing what M2 would do,
    `EvidenceKind.capture`'s docstring, and `<meta name="viewport">` in the HTML
    report -- the report's own responsive design.
    """
    benign = (
        '"""survives a screenshot, a pipe into a file, and a colourless terminal."""\n',
        "# M2's browser lens verifies it by booting the app\n"
        "def unrelated():\n    pass\n",
        'TEMPLATE = \'<meta name="viewport" content="width=device-width">\'\n',
    )
    for source in benign:
        names = _identifiers(ast.parse(source))
        assert not any(
            frag in n.lower() for n in names for frag in _BROWSER_IDENTIFIERS
        ), source


def test_every_argued_entry_says_why_a_third_lens_would_need_it():
    """The allowlist is where this gate would be quietly defeated, so an entry
    costs a sentence rather than a name.

    TWO ACCEPTABLE ARGUMENTS, and the second was added when the control plane
    arrived. The original gate assumed every consumer of these words is a lens,
    so the only way out was "a third lens would need this too". `whetstone ui`
    is not a lens at all -- it never touches `LensPack`, produces no candidates
    and yields no evidence -- and `webbrowser.open` hands a URL to the
    operating system rather than driving anything. That assumption was
    incomplete rather than wrong, so the second argument is admitted
    explicitly, with its own required words, instead of being smuggled in by
    phrasing an unrelated reason to contain "any lens".
    """
    third_lens = ("third lens", "ANY lens", "any lens")
    not_a_lens = ("is not a lens", "not a lens and")
    for key, why in _ARGUED_SPINE_KNOWLEDGE.items():
        assert why and len(why.split()) >= 10, key
        assert any(phrase in why for phrase in third_lens + not_a_lens), (
            f"{key}: the reason must argue EITHER that a lens other than "
            "rendered-ui would need this, OR that the module is not a lens at "
            "all. If neither is true, it is an accommodation."
        )


def test_every_argued_entry_is_still_present():
    """An entry for an identifier that is gone is an exemption nobody removed."""
    stale = []
    for (suffix, name), _why in _ARGUED_SPINE_KNOWLEDGE.items():
        path = _SRC / suffix
        if not path.exists():
            stale.append(f"{suffix} (missing)")
            continue
        if name not in _identifiers(ast.parse(path.read_text(encoding="utf-8"))):
            stale.append(f"{suffix}:{name}")
    assert stale == [], f"argued but no longer present: {stale}"


def test_the_lens_contract_is_unchanged_by_a_second_lens():
    """A second pack must fit the protocol that already exists.

    `LensPack` is what a third-party pack implements, so widening it for
    `rendered-ui` would mean the abstraction only ever fitted one lens -- and
    would break every pack already written against it. Annotations are read as
    well as attributes: `max_autonomy` is an annotation with no value and does
    not appear in `dir()`, so checking only `dir()` would miss a field added the
    same way.
    """
    from whetstone.lenses.base import LensPack

    surface = {n for n in dir(LensPack) if not n.startswith("_")}
    surface |= set(getattr(LensPack, "__annotations__", {}))
    assert surface == {"name", "max_autonomy", "supports_tier", "run"}, surface


def test_run_context_carries_nothing_browser_shaped():
    """The tempting leak, named explicitly.

    Not `import playwright` in `runner.py` -- nobody would write that. It is a
    `viewport` or `routes` field appearing on `RunContext` because putting it in
    `lens_options` felt indirect. `RunContext.options` is the mechanism that
    already exists for exactly this, and `code-defects` uses it for
    `sandbox_image`.
    """
    from whetstone.lenses.base import RunContext

    fields = {f.name for f in dataclasses.fields(RunContext)}
    assert fields == {
        "project_root",
        "state_root",
        "files",
        "tier",
        "lens_options",
        "run_id",
        "_skips",
    }, fields


def test_the_runner_dispatches_by_protocol_and_not_by_lens_name():
    """A spine that special-cases a lens NAME has stopped being lens-agnostic
    whatever its imports say, and it is the form hardest to notice."""
    tree = ast.parse((_SRC / "runner.py").read_text(encoding="utf-8"))
    compared = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(
            lens in ast.unparse(node)
            for lens in ("'rendered-ui'", '"rendered-ui"', "'product-ux'")
        )
    ]
    assert compared == [], (
        f"runner.py branches on a lens name: {compared}. The spine dispatches "
        "through the LensPack protocol."
    )
