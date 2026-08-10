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

# A key is secret-shaped when its lowercased name CONTAINS one of these tokens.
# Exact-name matching missed every conventional spelling — `github_token`,
# `client_secret`, `aws_secret_access_key`, `credentials` all sailed through.
_SECRET_SUBSTRINGS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "apikey",
)

# ...or when its final separator-delimited component is `key`: `api_key`,
# `private_key`, `signing-key`. A bare `key`, and `key` as a substring, are
# deliberately NOT enough — `keys`, `keyword`, `monkey` are ordinary config, and
# a tool that rejects someone's valid config is its own defect.
_SECRET_KEY_SUFFIX = re.compile(r"[_\-.]key$")


def find_config(start: Path) -> Path:
    """Walk up from *start* looking for whetstone.yaml."""
    start = start.resolve()
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"No {CONFIG_NAME} found in {start} or any parent directory.\n"
        "Run `whetstone init` to create one."
    )


def load_config(path: Path) -> WhetstoneConfig:
    """Load and validate the config at *path*."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
        raise ConfigError(f"{path} is invalid:\n{_redact(str(exc), resolved_secrets)}") from exc


def _is_secret_key(key: object) -> bool:
    lowered = str(key).lower()
    return any(token in lowered for token in _SECRET_SUBSTRINGS) or bool(
        _SECRET_KEY_SUFFIX.search(lowered)
    )


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

    Length-descending so a value that contains another is replaced first. Every
    resolved value is scrubbed regardless of length: short ones make for a
    noisier message, but guessing which values are "secret enough" is how leaks
    happen. The Pydantic detail is preserved, so the message still names the key.
    """
    for value in sorted(resolved, key=len, reverse=True):
        text = text.replace(value, resolved[value])
    return text
