import whetstone
from whetstone.errors import ConfigError, LiteralSecretError, WhetstoneError


def test_version_is_exposed():
    assert whetstone.__version__ == "0.0.1"


def test_every_error_descends_from_base():
    assert issubclass(ConfigError, WhetstoneError)
    assert issubclass(LiteralSecretError, ConfigError)
