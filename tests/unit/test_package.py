import re

import pytest

import whetstone
from whetstone.errors import ConfigError, LiteralSecretError, WhetstoneError

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
