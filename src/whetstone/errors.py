"""Every failure mode in Whetstone has a named type. Bare exceptions are a bug."""

from __future__ import annotations


class WhetstoneError(Exception):
    """Base for every error Whetstone raises deliberately."""


class ConfigError(WhetstoneError):
    """whetstone.yaml is missing, unparseable, or invalid."""


class LiteralSecretError(ConfigError):
    """A secret-shaped config key holds a literal value instead of a reference."""


class UnsafeConfigTargetError(WhetstoneError):
    """The whetstone.yaml path is a symlink, so writing it would leave the worktree."""


class UnsafeStatePathError(WhetstoneError):
    """The resolved state directory looks cloud-synced; SQLite would be torn."""


class StateDirError(WhetstoneError):
    """The state directory could not be created, or is not a directory."""


class SchemaVersionError(WhetstoneError):
    """The database on disk was stamped by a different schema version.

    There is no migration path yet, so this is refused loudly rather than
    letting CREATE TABLE IF NOT EXISTS no-op against a shape it doesn't
    recognise.
    """


class StoreError(WhetstoneError):
    """The findings store reached a state its own invariants forbid.

    Distinct from sqlite3's errors: those say the database refused a statement,
    this says the statement succeeded and touched the wrong number of rows.
    """


class GitError(WhetstoneError):
    """A git invocation failed."""


class CommandFailed(WhetstoneError):
    """A user-declared command exited non-zero or timed out."""


class LensError(WhetstoneError):
    """A lens pack misbehaved — bad contract, unhandled failure."""
