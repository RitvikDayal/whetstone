import textwrap
from pathlib import Path

import pytest

from whetstone.config.loader import CONFIG_NAME, find_config, load_config
from whetstone.config.model import Tier
from whetstone.errors import ConfigError, LiteralSecretError

MINIMAL = """\
version: 1
project:
  name: demo
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / CONFIG_NAME
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_minimal_config_loads_with_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL))
    assert cfg.project.name == "demo"
    assert cfg.budget.tier is Tier.quick
    assert cfg.boundaries.include == ["**/*"]
    assert [s.kind for s in cfg.sinks] == ["dashboard"]


def test_find_config_walks_up_to_parents(tmp_path):
    _write(tmp_path, MINIMAL)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_config(nested).parent == tmp_path


def test_find_config_reports_clearly_when_absent(tmp_path):
    with pytest.raises(ConfigError, match="whetstone init"):
        find_config(tmp_path)


def test_literal_secret_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        environment:
          app:
            auth: { kind: form, password: hunter2 }
        """,
    )
    with pytest.raises(LiteralSecretError, match="password"):
        load_config(path)


def test_env_reference_is_interpolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_PW", "s3cret")
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        environment:
          app:
            auth: { kind: form, password: "${env:DEMO_PW}" }
        """,
    )
    cfg = load_config(path)
    assert cfg.environment.app.auth["password"] == "s3cret"


def test_missing_env_reference_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        environment:
          app:
            auth: { kind: form, token: "${env:NOT_SET_ANYWHERE}" }
        """,
    )
    with pytest.raises(ConfigError, match="NOT_SET_ANYWHERE"):
        load_config(path)


def test_runtime_placeholders_survive_untouched(tmp_path):
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        environment:
          commands: { dev: "pnpm dev --port ${WHETSTONE_PORT}" }
        """,
    )
    cfg = load_config(path)
    assert cfg.environment.commands.dev == "pnpm dev --port ${WHETSTONE_PORT}"


def test_autonomy_above_three_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        lenses:
          hygiene: { enabled: true, autonomy: 4 }
        """,
    )
    with pytest.raises(ConfigError, match="no level 4"):
        load_config(path)


def test_unknown_top_level_key_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        lensez: {}
        """,
    )
    with pytest.raises(ConfigError):
        load_config(path)
