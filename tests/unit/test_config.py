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


def test_unknown_key_in_a_sink_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        sinks:
          - { kind: dashboard, labls: [x] }
        """,
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_env_reference_is_case_insensitive_on_the_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_PW", "s3cret")
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        environment:
          app:
            auth: { kind: form, password: "${ENV:DEMO_PW}" }
        """,
    )
    assert load_config(path).environment.app.auth["password"] == "s3cret"


def test_runtime_placeholder_named_env_something_is_untouched(tmp_path):
    """${ENVIRONMENT} is not an env reference - no colon, so it must survive."""
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        environment:
          commands: { dev: "serve --mode ${ENVIRONMENT}" }
        """,
    )
    assert load_config(path).environment.commands.dev == "serve --mode ${ENVIRONMENT}"


def test_non_utf8_config_is_a_config_error(tmp_path):
    path = tmp_path / CONFIG_NAME
    path.write_bytes(b"version: 1\nproject: { name: d\xffemo }\n")
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(path)


def test_missing_config_file_is_a_config_error(tmp_path):
    """find_config returns a path that load_config re-opens; it can vanish."""
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(tmp_path / CONFIG_NAME)


def test_config_path_that_is_a_directory_is_a_config_error(tmp_path):
    directory = tmp_path / CONFIG_NAME
    directory.mkdir()
    with pytest.raises(ConfigError, match="could not be read"):
        load_config(directory)


def test_resolved_secret_never_reaches_the_error_text(tmp_path, monkeypatch):
    """A typo elsewhere in the config must not print the resolved secret.

    `budget.tier` is validated against an enum, so a bogus value there fails
    Pydantic validation and the whole input is rendered into the message.
    """
    secret = "ghp_R3alSecretValue0000000000000000000000"
    monkeypatch.setenv("DEMO_GH_TOKEN", secret)
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        budget:
          tier: "${env:DEMO_GH_TOKEN}"
        """,
    )
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    rendered = str(caught.value)
    assert secret not in rendered
    assert "${env:DEMO_GH_TOKEN}" in rendered
    # The message must still say which key was wrong.
    assert "tier" in rendered


def test_redaction_covers_a_secret_under_a_misspelled_key(tmp_path, monkeypatch):
    """`extra="forbid"` renders the rejected value, so a typo'd key leaks it."""
    secret = "AKIAIOSFODNN7EXAMPLESECRET"
    monkeypatch.setenv("DEMO_AWS", secret)
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        sinks:
          - { kind: dashboard, api_tokn: "${env:DEMO_AWS}" }
        """,
    )
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    rendered = str(caught.value)
    assert secret not in rendered
    assert "api_tokn" in rendered


@pytest.mark.parametrize(
    "key",
    [
        "access_token",
        "github_token",
        "client_secret",
        "secret_key",
        "api_secret",
        "auth_token",
        "aws_secret_access_key",
        "credentials",
        "password",
        "api_key",
        "private_key",
        "apikey",
        "passwd",
    ],
)
def test_secret_shaped_keys_reject_literals(tmp_path, key):
    path = _write(
        tmp_path,
        f"""
        version: 1
        project: {{ name: demo }}
        environment:
          app:
            auth: {{ kind: form, {key}: literalvalue }}
        """,
    )
    with pytest.raises(LiteralSecretError, match=key):
        load_config(path)


@pytest.mark.parametrize(
    "key",
    ["kind", "keyword", "keys", "base_branch", "viewports", "monkey", "key"],
)
def test_innocent_keys_are_not_treated_as_secrets(tmp_path, key):
    """Over-matching would reject valid config in someone else's repository."""
    path = _write(
        tmp_path,
        f"""
        version: 1
        project: {{ name: demo }}
        environment:
          app:
            auth: {{ {key}: plainvalue }}
        """,
    )
    assert load_config(path).environment.app.auth[key] == "plainvalue"


@pytest.mark.parametrize(
    "literal",
    [
        "1234567890",
        "{ value: literalsecret }",
        "[literalsecret]",
        "true",
        "null",
        '["${env:DEMO_PW}"]',
    ],
)
def test_non_string_under_a_secret_key_is_rejected(tmp_path, literal, monkeypatch):
    monkeypatch.setenv("DEMO_PW", "s3cret")
    path = _write(
        tmp_path,
        f"""
        version: 1
        project: {{ name: demo }}
        environment:
          app:
            auth: {{ kind: form, password: {literal} }}
        """,
    )
    with pytest.raises(LiteralSecretError, match="password"):
        load_config(path)


def test_secret_key_message_names_the_required_form(tmp_path):
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        environment:
          app:
            auth: { github_token: 1234567890 }
        """,
    )
    with pytest.raises(LiteralSecretError) as caught:
        load_config(path)
    assert "${env:VAR_NAME}" in str(caught.value)
    assert "github_token" in str(caught.value)


@pytest.mark.parametrize(
    "section,doc",
    [
        (
            "environment",
            "version: 1\nproject: { name: demo }\nenvironment: { commnds: {} }\n",
        ),
        (
            "budget",
            "version: 1\nproject: { name: demo }\nbudget: { teir: quick }\n",
        ),
        (
            "boundaries",
            "version: 1\nproject: { name: demo }\nboundaries: { includ: ['a'] }\n",
        ),
        (
            "project",
            "version: 1\nproject: { name: demo, forg: {} }\n",
        ),
    ],
)
def test_unknown_key_in_a_nested_section_is_rejected(tmp_path, section, doc):
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError):
        load_config(path)
