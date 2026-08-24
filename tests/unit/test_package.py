import re
import zipfile
from pathlib import Path

import pytest

import whetstone
from whetstone.errors import ConfigError, LiteralSecretError, WhetstoneError

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The versions this project ships, and nothing else.
#
# `[0-9]`, NEVER `\d`. Python's `\d` matches every Unicode decimal digit, so
# `1<ARABIC-INDIC TWO>.3.4` satisfies it -- the same class of bug this project
# fixed in `hunt.py` this week, where `str.isdigit()` accepted a superscript and
# `int()` then refused it. Reintroduced here in a regex, which is how that class
# survives: it is fixed per-site rather than per-idea.
#
# `packaging.version` was the obvious instrument and is wrong twice over: it is
# not a declared dependency, so the proof would rest on something transitive
# that can vanish from the lock, and it NORMALISES `01.2.3` to `1.2.3` rather
# than refusing it -- it would not have caught one of the cases that prompted
# this test.
_DIGITS = "(0|[1-9][0-9]*)"
_VERSION = re.compile(
    rf"{_DIGITS}\.{_DIGITS}\.{_DIGITS}((a|b|rc)[0-9]+|\.post[0-9]+|\.dev[0-9]+)?"
)


@pytest.mark.parametrize(
    "bad",
    [
        "1.2.3garbage",
        "01.2.3",
        "1.2",
        "1.2.3.4",
        "v1.2.3",
        "",
        "0.0.0+unknown",
        # ARABIC-INDIC DIGIT TWO. `\d` matches it; `[0-9]` does not.
        "1٢.3.4",
        "١.٢.٣",
    ],
    ids=[
        "trailing-garbage", "leading-zero", "too-short", "too-long",
        "v-prefix", "empty", "unknown-fallback", "arabic-indic-digit",
        "all-arabic-indic",
    ],
)
def test_the_version_pattern_rejects_what_it_should(bad):
    """THE PATTERN IS ASSERTED TO DISCRIMINATE, not just to match. A version
    check that accepts anything is the same defect as no version check, and the
    first draft of it accepted `1.2.3garbage` and `01.2.3`."""
    assert _VERSION.fullmatch(bad) is None, bad


@pytest.mark.parametrize("good", ["0.1.0", "1.0.0", "0.0.1", "2.10.3", "1.0.0rc1"])
def test_the_version_pattern_accepts_what_it_should(good):
    """And it must not be so strict that a legitimate release cannot pass it."""
    assert _VERSION.fullmatch(good) is not None, good


def test_version_is_exposed():
    """A SHAPE, NOT A CONSTANT. This asserted `== "0.0.1"` -- a third hardcoded
    copy of the version, after pyproject.toml and `__init__.py`, which had to be
    hand-edited on every release and which restates the implementation rather
    than testing it. That the reported version AGREES with the packaged one is a
    real claim and lives in `test_the_reported_version_is_the_packaged_version`.
    """
    assert isinstance(whetstone.__version__, str)
    assert _VERSION.fullmatch(whetstone.__version__), whetstone.__version__
    # Belt and braces on the one bad value that is not a typo but a state: the
    # package metadata was unreadable and the version is a placeholder.
    assert "unknown" not in whetstone.__version__


def test_every_error_descends_from_base():
    assert issubclass(ConfigError, WhetstoneError)
    assert issubclass(LiteralSecretError, ConfigError)


# --- the built wheel actually contains the control plane ---------------------
#
# THIS IS THE GATE, not `hatch_build.py`. The hook is a convenience that does
# not even run on the main install path (`pip install` from a wheel never
# invokes a build backend). What stops a wheel shipping an empty UI is reading
# the wheel.
#
# Measured, twice, and both measurements changed the packaging:
#   1. `.gitignore` carries an unanchored `dist/`, and hatchling's file
#      selection is VCS-ignore-aware, so the built bundle was excluded from
#      both wheel and sdist. Fixed with `[tool.hatch.build] artifacts`.
#   2. With that fixed, the wheel then carried the entire `node_modules` tree --
#      over three thousand entries. Fixed with an explicit `exclude`.
# Neither was predicted. Both were found by opening the zip.


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory) -> list[str]:
    """The entry names of a REAL wheel, built once for this module.

    NO SKIP BRANCH. A test that skips when it cannot find a wheel is exactly
    the "check that quietly does not run" defect, and this one guards a
    silently-empty release artifact. `build` and `hatchling` are dev
    dependencies so this always has what it needs.

    `WHETSTONE_SKIP_UI_BUILD` is deliberately NOT set: the point is to exercise
    the real path including the build hook. If the bundle is already built the
    hook returns immediately; if it is not and npm is present, it builds it.

    Module-scoped because a wheel build is seconds, not milliseconds, and both
    assertions below read the same artifact.
    """
    import build

    into = tmp_path_factory.mktemp("wheel")
    wheel = Path(build.ProjectBuilder(str(_REPO_ROOT)).build("wheel", str(into)))
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


def test_the_built_wheel_contains_the_control_plane(wheel_names):
    names = wheel_names

    assert "whetstone/ui/dist/index.html" in names, (
        "the wheel shipped without the control plane's entry point. Every "
        "`whetstone ui` from this wheel would refuse to start."
    )
    hashed = [
        n
        for n in names
        if n.startswith("whetstone/ui/dist/assets/") and n.endswith(".js")
    ]
    assert hashed, (
        f"index.html shipped with no JavaScript beside it: {names[:20]}. A "
        "shell with no bundle renders an empty page and looks like a routing "
        "bug."
    )


def test_the_wheel_does_not_ship_node_modules_or_front_end_source(wheel_names):
    """A wheel is not a development checkout.

    The first build with `artifacts` declared carried `node_modules` in full --
    `.bin` shims, a nested lockfile, every transitive package. Nothing under
    `ui/` except `dist/` is importable, installable, or useful at runtime.
    """
    names = wheel_names

    stowaways = [
        n
        for n in names
        if n.startswith("whetstone/ui/") and not n.startswith("whetstone/ui/dist/")
    ]
    assert stowaways == [], f"the wheel carries front-end files it does not need: {stowaways[:15]}"
    assert not any("node_modules" in n for n in names)
