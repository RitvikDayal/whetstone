"""Whetstone -- evidence-gated project improvement."""

from __future__ import annotations

# READ FROM THE INSTALLED METADATA, never written here. Two version strings in
# two files drift, and this pair drifts SILENTLY: the release workflow checks
# the git tag against the built wheel, so a wheel correctly labelled 0.1.0
# whose `whetstone version` still says 0.0.1 passes every gate there is. Caught
# exactly that way -- the bump landed in pyproject.toml and the CLI kept
# reporting the old number.
#
# The fallback is deliberately not a plausible version. Running from a source
# tree that was never installed is a real situation, and "0.0.0+unknown" says
# so instead of asserting a release that does not exist.
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _version

    __version__ = _version("whetstone-cli")
except PackageNotFoundError:  # pragma: no cover - only outside an install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
