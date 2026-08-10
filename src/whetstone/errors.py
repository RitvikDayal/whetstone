"""Every failure mode in Whetstone has a named type. Bare exceptions are a bug."""

from __future__ import annotations


class WhetstoneError(Exception):
    """Base for every error Whetstone raises deliberately."""


class ConfigError(WhetstoneError):
    """whetstone.yaml is missing, unparseable, or invalid."""


class LiteralSecretError(ConfigError):
    """A secret-shaped config key holds a literal value instead of a reference."""


class UnsafeStatePathError(WhetstoneError):
    """The resolved state directory looks cloud-synced; SQLite would be torn."""


class GitError(WhetstoneError):
    """A git invocation failed."""


class CommandFailed(WhetstoneError):
    """A user-declared command exited non-zero or timed out."""


class LensError(WhetstoneError):
    """A lens pack misbehaved — bad contract, unhandled failure."""
