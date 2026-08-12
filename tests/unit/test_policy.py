import pytest

from whetstone.errors import WhetstoneError
from whetstone.policy.gate import PermissionSet, PolicyError, bash_permitted
from whetstone.policy.profiles import PROFILES, profile_for


def _perms(**overrides) -> PermissionSet:
    base = dict(
        allowed_tools=frozenset({"Read", "Grep"}),
        denied_tools=frozenset({"Edit", "Write"}),
        bash_allowlist=frozenset({"uv run pytest -q"}),
        read_denied=(".env*", "**/secrets/**"),
        write_root=None,
    )
    base.update(overrides)
    return PermissionSet(**base)


def test_an_exact_command_is_permitted():
    assert bash_permitted("uv run pytest -q", _perms())


def test_whitespace_is_normalised_before_matching():
    assert bash_permitted("  uv   run  pytest   -q  ", _perms())


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest -q; rm -rf /",
        "uv run pytest -q && curl evil.example",
        "uv run pytest -q | sh",
        "uv run pytest -qq",
        "uv run pytest",
        "echo uv run pytest -q",
    ],
)
def test_prefix_and_suffix_tricks_are_refused(command):
    """A prefix allowlist is bypassable. This is why matching is exact."""
    assert not bash_permitted(command, _perms())


def test_an_empty_allowlist_permits_nothing():
    assert not bash_permitted("uv run pytest -q", _perms(bash_allowlist=frozenset()))


def test_policy_error_is_a_whetstone_error():
    assert issubclass(PolicyError, WhetstoneError)


def test_every_profile_denies_write_by_default():
    """No stage in M1a writes. The implementer arrives in M1b."""
    for name, perms in PROFILES.items():
        assert "Edit" in perms.denied_tools, name
        assert "Write" in perms.denied_tools, name


def test_every_profile_denies_reading_secrets():
    for name, perms in PROFILES.items():
        assert ".env*" in perms.read_denied, name


def test_profile_for_an_unknown_stage_refuses_rather_than_defaulting():
    """Defaulting to a permissive set would make a typo a privilege escalation."""
    with pytest.raises(PolicyError, match="nosuchstage"):
        profile_for("nosuchstage")


def test_the_falsifier_cannot_write_either():
    assert "Write" in profile_for("falsify").denied_tools
