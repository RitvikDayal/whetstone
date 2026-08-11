"""Find, parse, secret-check, interpolate, and validate whetstone.yaml."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..errors import ConfigError, LiteralSecretError
from .model import WhetstoneConfig

CONFIG_NAME = "whetstone.yaml"

# Matches ${env:VAR_NAME} (env prefix case-insensitive, variable name is not —
# environment variable names are case-sensitive). Deliberately narrower than
# ${VAR} so that runtime placeholders Whetstone substitutes itself —
# ${WHETSTONE_PORT} — pass through.
_ENV_REF = re.compile(r"\$\{[Ee][Nn][Vv]:([A-Za-z_][A-Za-z0-9_]*)\}")

# Words that name a secret. Matched as whole WORDS, never as substrings.
# Substring matching caught every conventional spelling and a pile of ordinary
# config with it — `max_tokens`, `tokenizer`, `secretary`, `credentialing`,
# `passwords_enabled` were all rejected, and a tool that will not load someone's
# valid config is its own defect.
#
# Plurals are deliberately absent, `credentials` excepted. A plural names a
# quantity or a collection rather than one secret value: `max_tokens` is an LLM
# setting, `passwords_enabled` a feature flag. Demanding `${env:...}` there
# makes ordinary documents unloadable with no escape hatch. `credentials` stays
# because that is simply how the thing is spelled — nobody writes `credential:`
# for an auth blob.
_SECRET_WORDS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "credential",
        "credentials",
        "apikey",
        "key",
    }
)

# Splits a key into words. Separators (`_`, `-`, `.`) fall away; camelCase and
# ACRONYMCase split on the case boundary. `sessionSecret` -> session, Secret;
# `AWS_SECRET_ACCESS_KEY` -> AWS, SECRET, ACCESS, KEY.
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def find_config(start: Path) -> Path:
    """Walk up from *start* looking for whetstone.yaml.

    The walk stops at the git worktree root. Without that stop, a whetstone.yaml
    in a parent directory or in $HOME silently supplies someone else's
    `never_touch` write barrier and `state_dir` to this run. When there is no
    repository the walk continues to the filesystem root as before.
    """
    start = start.resolve()
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
        # `.git` is a FILE inside a linked worktree or a submodule, so test for
        # existence rather than for a directory. Checked after the candidate so
        # a config sitting at the repo root is still found.
        if (directory / ".git").exists():
            raise ConfigError(
                f"No {CONFIG_NAME} found between {start} and the repository "
                f"root {directory}.\n"
                "The search stops there on purpose: a config outside the "
                "repository would apply another project's write barrier to this "
                "one.\n"
                "Run `whetstone init` to create one."
            )
    raise ConfigError(
        f"No {CONFIG_NAME} found in {start} or any parent directory.\n"
        "Run `whetstone init` to create one."
    )


def load_config(path: Path) -> WhetstoneConfig:
    """Load and validate the config at *path*."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid UTF-8 (byte {exc.start}: {exc.reason}).\n"
            "Re-save it as UTF-8 without a byte-order mark."
        ) from exc
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{path} does not exist.\nRun `whetstone init` to create one."
        ) from exc
    except OSError as exc:
        # Covers IsADirectoryError, PermissionError, and the assorted platform
        # spellings of the same two mistakes — Windows raises PermissionError
        # where POSIX raises IsADirectoryError for a directory.
        detail = "it is a directory" if path.is_dir() else str(exc)
        raise ConfigError(f"{path} could not be read: {detail}") from exc

    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level.")

    _reject_literal_secrets(raw, [])
    # Interpolation puts real secret values into the tree. Anything that
    # stringifies that tree afterwards — Pydantic renders `input_value=` verbatim
    # — would print them, so record what was resolved and scrub it back out.
    resolved_secrets: dict[str, str] = {}
    resolved = _interpolate(raw, resolved_secrets)

    try:
        return WhetstoneConfig.model_validate(resolved)
    except ValidationError as exc:
        detail = _redact(str(exc), resolved_secrets)

    # Raised OUTSIDE the `except` block on purpose. Inside it, Python attaches
    # the ValidationError as __context__ (and `from exc` would attach it as
    # __cause__), and that exception renders the secret unredacted. The default
    # traceback hook, `logging.exception`, and `traceback.format_exc` all walk
    # the chain and print it directly above the redacted message. Once the block
    # exits, the ValidationError is no longer the exception being handled, so
    # nothing chains it and the redacted text is the only rendering that exists.
    raise ConfigError(f"{path} is invalid:\n{detail}")


def _is_secret_key(key: object) -> bool:
    """True when the key names a secret value, judged word by word."""
    words = [word.lower() for word in _WORDS.findall(str(key))]
    if not words:
        return False
    # A secret word after the head names the value itself: `github_token`,
    # `aws_secret_access_key`, `sessionSecret`, `github_token_v2`. In head
    # position the same word qualifies what follows instead of naming it —
    # `token_budget` is a budget, `secret_scanning` a feature — so it does not
    # count there. `secret_key` and `api_key` are still caught, by `key`.
    if any(word in _SECRET_WORDS for word in words[1:]):
        return True
    # A one-word key names the value directly: `password`, `credentials`.
    # `key` is the exception in every position: `key`/`keys` are how mappings
    # are described everywhere, and rejecting them would be over-matching again.
    return len(words) == 1 and words[0] != "key" and words[0] in _SECRET_WORDS


def _reject_literal_secrets(node: Any, trail: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            here = [*trail, str(key)]
            # A secret-shaped key must hold exactly one ${env:...} string.
            # Numbers, lists, mappings, booleans, and null are all ways to smuggle
            # a literal past an `isinstance(value, str)` guard.
            if _is_secret_key(key) and not (
                isinstance(value, str) and _ENV_REF.fullmatch(value.strip())
            ):
                raise LiteralSecretError(
                    f"{'.'.join(here)} must hold a single environment reference, "
                    f"not {_describe(value)}.\n"
                    "Secrets must be references so they never land in version "
                    'control: use "${env:VAR_NAME}".'
                )
            _reject_literal_secrets(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _reject_literal_secrets(value, [*trail, str(index)])


def _describe(value: Any) -> str:
    """Name the shape of a rejected value without ever echoing it."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, dict):
        return "a mapping"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, str):
        return "a literal string"
    return f"a literal {type(value).__name__}"


def _interpolate(node: Any, resolved: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {key: _interpolate(value, resolved) for key, value in node.items()}
    if isinstance(node, list):
        return [_interpolate(value, resolved) for value in node]
    if isinstance(node, str):
        return _ENV_REF.sub(lambda match: _substitute(match, resolved), node)
    return node


def _substitute(match: re.Match[str], resolved: dict[str, str]) -> str:
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise ConfigError(
            f"Config references ${{env:{name}}} but {name} is not set in the "
            "environment."
        )
    if value:
        resolved[value] = f"${{env:{name}}}"
    return value


def _redact(text: str, resolved: dict[str, str]) -> str:
    """Put every resolved environment value back behind its reference.

    Both spellings of each value are scrubbed. Pydantic renders the rejected
    input with repr(), which escapes control characters, so a secret holding a
    newline reaches the message as `line1\\nSECRET` -- text that never matches
    the raw value and sailed straight through a plain str.replace. A trailing
    newline is the ordinary case, not an exotic one: SECRET=$(cat token.txt).

    Length-descending so a value that contains another is replaced first. Every
    resolved value is scrubbed regardless of length: short ones make for a
    noisier message, but guessing which values are "secret enough" is how leaks
    happen. The Pydantic detail is preserved, so the message still names the key.
    """
    spellings: dict[str, str] = {}
    for value, reference in resolved.items():
        spellings[value] = reference
        # repr() quotes the value; strip the delimiters and keep the escaping.
        spellings.setdefault(repr(value)[1:-1], reference)
    for spelling in sorted(spellings, key=len, reverse=True):
        text = text.replace(spelling, spellings[spelling])
    # Exact replacement cannot catch a value Pydantic ELIDED. Past ~50
    # characters it renders `head...tail`, so neither spelling matches and both
    # ends print verbatim -- a 51-character credential leaked 24 characters off
    # each end. The split point is a Pydantic internal, so sweep for surviving
    # runs instead of hard-coding it.
    for value, reference in resolved.items():
        text = _scrub_runs(text, value, reference)
        text = _scrub_runs(text, repr(value)[1:-1], reference)
    return text


# Below this, a shared run is more likely to be a coincidence than a leak, and
# over-redacting the message has its own cost. Above it, assume the worst.
_MIN_LEAKED_RUN = 8


def _scrub_runs(text: str, value: str, reference: str) -> str:
    """Replace every run of *value* at least _MIN_LEAKED_RUN long left in *text*."""
    start = 0
    while start <= len(value) - _MIN_LEAKED_RUN:
        if value[start : start + _MIN_LEAKED_RUN] not in text:
            start += 1
            continue
        end = start + _MIN_LEAKED_RUN
        while end < len(value) and value[start : end + 1] in text:
            end += 1
        text = text.replace(value[start:end], reference)
        start = end
    return text
