import random
import textwrap
import traceback
from pathlib import Path

import pytest

from whetstone.config.loader import CONFIG_NAME, _scrub_runs, find_config, load_config
from whetstone.config.model import OnCeiling, Tier, Trust
from whetstone.errors import ConfigError, LiteralSecretError
from whetstone.lenses.base import Severity, severity_at_least

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


def test_find_config_stops_at_the_worktree_root(tmp_path):
    """Someone else's config in a parent must not supply this run's never_touch."""
    _write(tmp_path, MINIMAL)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".git").mkdir()
    with pytest.raises(ConfigError, match="repository root"):
        find_config(repo / "src")


def test_find_config_stops_at_a_dot_git_file(tmp_path):
    """Inside a worktree or submodule, .git is a file, not a directory."""
    _write(tmp_path, MINIMAL)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".git").write_text("gitdir: ../.git/worktrees/x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="repository root"):
        find_config(repo / "src")


def test_find_config_still_finds_one_inside_the_repo(tmp_path):
    _write(tmp_path, MINIMAL)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".git").mkdir()
    _write(repo, MINIMAL)
    assert find_config(repo / "src").parent == repo


def test_find_config_checks_the_repo_root_itself(tmp_path):
    """The walk stops AT the root, not before it."""
    repo = tmp_path / "repo"
    (repo / "src" / "deep").mkdir(parents=True)
    (repo / ".git").mkdir()
    _write(repo, MINIMAL)
    assert find_config(repo / "src" / "deep").parent == repo


def test_find_config_without_a_repo_keeps_walking(tmp_path):
    """No repository found means the old behaviour, unchanged."""
    _write(tmp_path, MINIMAL)
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert find_config(nested).parent == tmp_path


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


def _longest_surviving_run(text: str, secret: str) -> int:
    """Length of the longest contiguous slice of *secret* still present in *text*."""
    longest = 0
    for start in range(len(secret)):
        for end in range(start + longest + 1, len(secret) + 1):
            if secret[start:end] not in text:
                break
            longest = end - start
    return longest


def _assert_secret_is_gone(exc: BaseException, secret: str) -> None:
    """Fail if *secret* survives anywhere a default renderer would reach.

    Checking `str(exc)` alone is what let two leaks through. The value has to be
    absent from every rendering, in both the raw and the repr-escaped spelling,
    and the exception chain has to be empty -- an unredacted `__cause__` is
    printed by the default traceback hook, `logging.exception`, and
    `traceback.format_exc` alike.
    """
    escaped = repr(secret)[1:-1]
    renderings = {
        "str(exc)": str(exc),
        "repr(exc)": repr(exc),
        "traceback.format_exception": "".join(traceback.format_exception(exc)),
    }
    for where, text in renderings.items():
        assert secret not in text, f"raw secret reached {where}"
        assert escaped not in text, f"repr-escaped secret reached {where}"
        # Whole-value containment is not enough. Pydantic elides a long value in
        # the middle, so `secret not in text` passes while both ends print
        # verbatim. Assert that no usable run of the secret survives.
        run = _longest_surviving_run(text, secret)
        assert run < 8, f"a {run}-character run of the secret reached {where}"
    assert exc.__cause__ is None, "the unredacted ValidationError is still chained"
    assert exc.__context__ is None, "the unredacted ValidationError is still the context"


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_R3alSecretValue0000000000000000000000",
        # Pydantic renders values with repr(), so a control character comes out
        # escaped and `str.replace` against the raw value misses it entirely.
        "line1\nSUPERSECRETLINE2",
        "tabbed\tSECRETVALUE",
        # The ordinary way a secret grows a newline: SECRET=$(cat token.txt).
        "trailing_newline_SECRETVALUE\n",
        "carriage\rSECRETRETURN",
        "quoted'SECRET\"VALUE",
        # Past ~50 characters Pydantic elides the middle and renders
        # `head...tail`, so neither spelling matches and both ends survive.
        "AKIAIOSFODNN7EXAMPLE/wJalrXUtnFEMI/K7MDENG/bPxRfiCY",
        "ghp_" + "L" * 500,
        "-----BEGIN PRIVATE KEY-----\n" + "M" * 300 + "\n-----END KEY-----",
    ],
)
def test_resolved_secret_survives_nowhere_in_the_raised_error(
    tmp_path, monkeypatch, secret
):
    """Redacting `str(exc)` is not enough: the chain and the escaping defeat it."""
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
    _assert_secret_is_gone(caught.value, secret)
    # ...and the message is still useful. A redaction that shreds the error into
    # uselessness would pass every assertion above.
    rendered = str(caught.value)
    assert "${env:DEMO_GH_TOKEN}" in rendered
    assert "tier" in rendered


def test_redaction_does_not_depend_on_a_top_level_handler(tmp_path, monkeypatch):
    """There is no ConfigError handler anywhere, so this traceback is what users see."""
    secret = "ghp_UnhandledTracebackSecret000000000000"
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
    try:
        load_config(path)
    except ConfigError:
        printed = traceback.format_exc()
    assert secret not in printed
    assert "ValidationError" not in printed


_SECRET_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _nonperiodic_secret(length: int = 300, seed: int = 20260810) -> str:
    """A deterministic secret with no internal repetition.

    THE NON-PERIODICITY IS THE POINT, and a periodic secret makes every test
    below worthless. An earlier attempt at this exploit built its secret by
    repeating a 62-character alphabet. Every elided copy of a periodic value
    contains the same fragments as every other, so scrubbing any ONE copy
    scrubbed the rest by coincidence, the surviving-run assertion came back
    clean, and the bug read as absent while it was wide open. Seeded random
    keeps the test reproducible without buying that coincidence back.
    """
    rng = random.Random(seed)
    return "ghp_" + "".join(rng.choice(_SECRET_ALPHABET) for _ in range(length - 4))


def _three_references(*paddings: str, suffix: bool) -> str:
    """A config whose three bad keys each render the same secret at a different offset.

    Pydantic elides a long value to `head + "..." + tail`, so padding a reference
    by one more character shifts which slice of the secret survives elision. Three
    references therefore produce three overlapping copies at consecutive offsets --
    a family, not a queue. Scrubbing that consumed one copy and skipped past the
    rest left the others standing.
    """
    lines = ["version: 1", "project:", "  name: demo"]
    for index, pad in enumerate(paddings):
        body = f"${{env:DEMO_GH_TOKEN}}{pad}" if suffix else f"{pad}${{env:DEMO_GH_TOKEN}}"
        lines.append(f'typo{index}: "{body}"')
    return "\n".join(lines) + "\n"


def _rendered_error(tmp_path, monkeypatch, secret: str, doc: str) -> str:
    monkeypatch.setenv("DEMO_GH_TOKEN", secret)
    path = tmp_path / CONFIG_NAME
    path.write_text(doc, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    return str(caught.value)


def test_three_references_leak_no_run_of_the_secret_suffix_variant(
    tmp_path, monkeypatch
):
    """Three elided renderings of one secret; scrubbing used to skip two of them.

    Asserting `secret not in rendered` is the weak check that let all three
    previous leaks through -- Pydantic elides the middle, so the whole value is
    never present while both ends print verbatim. Assert on the longest surviving
    RUN instead. This shape leaked 21 consecutive characters into plain
    `str(exc)`, which every downstream surface inherits.
    """
    secret = _nonperiodic_secret()
    rendered = _rendered_error(
        tmp_path, monkeypatch, secret, _three_references("", "Z", "ZZ", suffix=True)
    )
    run = _longest_surviving_run(rendered, secret)
    assert run < 8, f"a {run}-character run of the secret survived"
    # ...and the message still names the keys and the reference to fix.
    assert "${env:DEMO_GH_TOKEN}" in rendered
    assert "typo1" in rendered


def test_three_references_leak_no_run_of_the_secret_prefix_variant(
    tmp_path, monkeypatch
):
    """Padding in front shifts the HEAD, which is the identifying half.

    The suffix variant leaks tail entropy; this one leaked 22 characters
    including the `ghp_` prefix that says which service the credential is for.
    """
    secret = _nonperiodic_secret()
    rendered = _rendered_error(
        tmp_path, monkeypatch, secret, _three_references("", "Z", "ZZ", suffix=False)
    )
    run = _longest_surviving_run(rendered, secret)
    assert run < 8, f"a {run}-character run of the secret survived"
    assert secret[:8] not in rendered, "the identifying head survived"
    assert "${env:DEMO_GH_TOKEN}" in rendered


@pytest.mark.parametrize("count", [1, 2, 3, 4, 6, 10, 16])
def test_no_number_of_references_leaks_a_run(tmp_path, monkeypatch, count):
    """Three references was where it was found, not where it stops.

    Ten prefix-padded references defeated the one-line cursor fix that closed the
    three-reference case: the copies differ in LENGTH as well as offset, and
    replacing the one maximal run leaves the shorter ones sharing its start.
    """
    secret = _nonperiodic_secret()
    paddings = tuple("Z" * i for i in range(count))
    for suffix in (True, False):
        rendered = _rendered_error(
            tmp_path, monkeypatch, secret, _three_references(*paddings, suffix=suffix)
        )
        run = _longest_surviving_run(rendered, secret)
        assert run < 8, f"{count} refs, suffix={suffix}: a {run}-character run survived"


def test_scrub_runs_takes_out_shorter_copies_sharing_a_start_offset():
    """The defect that survived both cursor rules, isolated from Pydantic.

    `text.replace(value[start:end], ...)` removes only the copies of that EXACT
    length. Here three copies of the head share offset 0 at three lengths; the
    greedy pass took the 20 and no cursor over the value's offsets ever came back
    for the 9 and the 12. Pydantic's elision rule is an internal that can change,
    so this states the invariant directly against the scrubber.
    """
    value = _nonperiodic_secret(40, seed=99)
    text = f"a={value[:20]!r} b={value[:9]!r} c={value[:12]!r}"
    scrubbed = _scrub_runs(text, value, "${env:DEMO_GH_TOKEN}")
    run = _longest_surviving_run(scrubbed, value)
    assert run < 8, f"a {run}-character run survived: {scrubbed}"


def test_scrub_runs_leaves_text_that_merely_resembles_the_secret(tmp_path):
    """Over-redaction passes every leak assertion, so pin the other side too."""
    value = _nonperiodic_secret(40, seed=7)
    text = "budget.tier: input should be 'quick', 'standard' or 'deep'"
    assert _scrub_runs(text, value, "${env:DEMO_GH_TOKEN}") == text


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
        # camelCase and SCREAMING_CASE are the same key in a different coat.
        "sessionSecret",
        "PASSWORD",
        "apiKey",
        "AWS_SECRET_ACCESS_KEY",
        "signing-key",
        "credential",
        # A trailing qualifier must not launder the secret word.
        "github_token_v2",
        "db.password",
        # A version or an index is the same word again, not a second word that
        # demotes `token` to a qualifier. All three were accepted outright:
        # `[a-z0-9]+` swallowed the digits, so `token2` was one word nobody had
        # put in the set, and `token_v2` split to ['token', 'v2'] where `token`
        # collected the head-position discount.
        "token2",
        "token_v2",
        "token_v10",
        "2token",
        "password2",
        "apiKey2",
        # `key` in company with a credential qualifier stays a credential.
        "signing_key",
        "access_key",
        "encryption_key",
        "master_key",
        "client_key",
        "session_key",
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
    [
        "kind",
        "keyword",
        "keys",
        "base_branch",
        "viewports",
        "monkey",
        "key",
        "monkey_patch",
        # Words that CONTAIN a secret token. Substring matching rejected every
        # one of these, which is the regression this list exists to catch.
        "tokenizer",
        "secretary",
        "credentialing",
        "passwordless",
        "keychain_path",
        # Plurals name a quantity or a collection, not one secret value.
        "max_tokens",
        "passwords_enabled",
        # A secret word in head position qualifies what follows; it does not
        # name the value. `token_budget` is a budget, not a token.
        "token_budget",
        "secret_scanning",
        "token_limit",
        # `key` on its own names a lookup, not a credential, and these are
        # ordinary inside the free-form dicts (`app.ready_when`, `app.auth`,
        # `model.stages`). Rejecting them made a valid config unloadable with no
        # escape hatch, which is the same defect as the substring matching that
        # came before.
        "sort_key",
        "primary_key",
        "foreign_key",
        "cache_key",
        "partition_key",
        "idempotency_key",
        "key_id",
        "api_keys",
        # Digits split off as their own word now; that must not turn ordinary
        # names into one-word secrets.
        "sha256",
        "oauth2",
        "max_tokens2",
    ],
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


def test_per_stage_model_settings_load(tmp_path):
    """`model.stages` is typed for exactly this, so Whetstone's own config uses it.

    Substring matching made `max_tokens` a secret-shaped key, which made this
    document unloadable with no escape hatch.
    """
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        model:
          provider: anthropic
          stages:
            triage: { max_tokens: 4096, token_budget: 200000, tokenizer: cl100k }
            repair: { max_tokens: 16384 }
        environment:
          app:
            auth: { kind: form, token_url: "https://example.test/oauth/token" }
        """,
    )
    cfg = load_config(path)
    assert cfg.model.stages["triage"]["max_tokens"] == 4096
    assert cfg.model.stages["repair"]["max_tokens"] == 16384
    assert cfg.environment.app.auth["token_url"].endswith("/token")


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


def test_severity_floor_is_validated_against_the_severity_enum(tmp_path):
    """'HIGH' used to validate here and become a KeyError in severity_at_least."""
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        lenses:
          hygiene: { severity_floor: HIGH }
        """,
    )
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert "severity_floor" in str(caught.value)
    assert "critical" in str(caught.value)


def test_severity_floor_round_trips_into_the_lens_enum(tmp_path):
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        lenses:
          hygiene: { severity_floor: high }
        """,
    )
    floor = load_config(path).lenses["hygiene"].severity_floor
    assert floor is Severity.high
    assert severity_at_least(Severity.critical, floor)
    assert not severity_at_least(Severity.low, floor)


def test_unknown_on_ceiling_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        budget: { on_ceiling: keep_going }
        """,
    )
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert "on_ceiling" in str(caught.value)
    assert "stop_and_report" in str(caught.value)


def test_unknown_trust_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        lenses:
          hygiene: { trust: yes_please }
        """,
    )
    with pytest.raises(ConfigError, match="assumed"):
        load_config(path)


def test_documented_lens_settings_are_accepted(tmp_path):
    path = _write(
        tmp_path,
        """
        version: 1
        project: { name: demo }
        budget: { on_ceiling: stop_and_report }
        lenses:
          hygiene: { enabled: true, autonomy: 3, trust: assumed }
        """,
    )
    cfg = load_config(path)
    assert cfg.budget.on_ceiling is OnCeiling.stop_and_report
    assert cfg.lenses["hygiene"].trust is Trust.assumed


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
