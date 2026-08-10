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

# Matches ${env:VAR_NAME}. Deliberately narrower than ${VAR} so that runtime
# placeholders Whetstone substitutes itself — ${WHETSTONE_PORT} — pass through.
_ENV_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")

_SECRET_KEYS = frozenset(
    {"password", "token", "secret", "api_key", "apikey", "private_key"}
)


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
    resolved = _interpolate(raw)

    try:
        return WhetstoneConfig.model_validate(resolved)
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{exc}") from exc


def _reject_literal_secrets(node: Any, trail: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            here = [*trail, str(key)]
            if (
                str(key).lower() in _SECRET_KEYS
                and isinstance(value, str)
                and not _ENV_REF.fullmatch(value.strip())
            ):
                raise LiteralSecretError(
                    f"{'.'.join(here)} contains a literal value.\n"
                    "Secrets must be references so they never land in version "
                    'control: use "${env:VAR_NAME}".'
                )
            _reject_literal_secrets(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _reject_literal_secrets(value, [*trail, str(index)])


def _interpolate(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _interpolate(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_interpolate(value) for value in node]
    if isinstance(node, str):
        return _ENV_REF.sub(_substitute, node)
    return node


def _substitute(match: re.Match[str]) -> str:
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise ConfigError(
            f"Config references ${{env:{name}}} but {name} is not set in the "
            "environment."
        )
    return value
