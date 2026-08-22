import whetstone
from whetstone.errors import ConfigError, LiteralSecretError, WhetstoneError


def test_version_is_exposed():
    """A SHAPE, NOT A CONSTANT. This asserted `== "0.0.1"` -- a third hardcoded
    copy of the version, after pyproject.toml and `__init__.py`, which had to be
    hand-edited on every release and which restates the implementation rather
    than testing it. That the reported version AGREES with the packaged one is
    a real claim and lives in `test_the_reported_version_is_the_packaged_version`.
    """
    import re

    assert isinstance(whetstone.__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", whetstone.__version__), (
        whetstone.__version__
    )
    assert not whetstone.__version__.startswith("0.0.0+unknown"), (
        "the package metadata was not readable, so the version is a placeholder"
    )


def test_every_error_descends_from_base():
    assert issubclass(ConfigError, WhetstoneError)
    assert issubclass(LiteralSecretError, ConfigError)
