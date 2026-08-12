import os
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

from whetstone.errors import ConfigError, StateDirError, UnsafeStatePathError
from whetstone.paths import (
    _unexpanded_variable,
    assert_not_cloud_synced,
    state_root,
)

ON_WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not ON_WINDOWS, reason="Windows path semantics")
posix_only = pytest.mark.skipif(ON_WINDOWS, reason="POSIX path semantics")


@pytest.mark.parametrize(
    "bad",
    [
        r"C:\Users\x\OneDrive\whetstone",
        r"C:\Users\x\OneDrive - Contoso\whetstone",
        r"C:\Users\x\Dropbox\state",
        "/Users/x/Library/CloudStorage/GoogleDrive-a/state",
        "/home/x/Google Drive/state",
        r"C:\Users\x\iCloudDrive\state",
        "/home/x/Nextcloud/state",
        "/home/x/Sync/pcloud/state",
        "/home/x/Syncthing/state",
        "/home/x/Box Sync/state",
        # Google Drive for Desktop mounts a lettered drive on Windows; these two
        # are the default layout and were both allowed before.
        r"G:\My Drive\projects\state",
        r"G:\Shared drives\team\state",
        # The real iCloud Drive path on macOS. The `icloud` marker only ever
        # caught the Windows `iCloudDrive` folder name.
        "/Users/x/Library/Mobile Documents/com~apple~CloudDocs/state",
        r"C:\Users\x\MEGA\s",
        r"C:\Users\x\Resilio Sync\s",
        r"C:\Users\x\Seafile\s",
        r"C:\Users\x\Tresorit\s",
        r"C:\Users\x\Sync.com\s",
    ],
)
def test_cloud_synced_paths_are_refused(bad):
    with pytest.raises(UnsafeStatePathError):
        assert_not_cloud_synced(Path(bad))


@pytest.mark.parametrize(
    "good",
    [
        r"C:\Users\x\.whetstone\abc123",
        "/home/x/.local/state/whetstone",
        # `mega` must be a path component, not a bare substring.
        "/home/x/omega/state",
        "/home/x/megabyte-bench/state",
        r"C:\Users\x\Projects\omega\state",
    ],
)
def test_ordinary_paths_are_allowed(good):
    assert_not_cloud_synced(Path(good))


def test_ordinary_path_is_allowed(tmp_path):
    assert_not_cloud_synced(tmp_path)


def test_state_root_is_stable_and_created(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    override = str(tmp_path / "state")
    first = state_root(project, override)
    second = state_root(project, override)
    assert first == second
    assert first.is_dir()


def test_state_root_differs_per_project(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    a = state_root(tmp_path / "alpha")
    b = state_root(tmp_path / "beta")
    assert a != b


def test_symlinked_ancestor_into_a_cloud_path_is_refused(tmp_path):
    cloud = tmp_path / "OneDrive"
    cloud.mkdir()
    link = tmp_path / "plain-looking"
    try:
        link.symlink_to(cloud, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        # Fallback: try Windows junction (mklink /J) which doesn't require elevation
        if sys.platform == "win32":
            try:
                subprocess.run(
                    f'mklink /J "{link}" "{cloud}"',
                    shell=True,
                    check=True,
                    capture_output=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as junction_exc:
                pytest.skip(
                    f"cannot create symlinks or junctions: symlink={exc}, junction={junction_exc}"
                )
        else:
            pytest.skip(f"cannot create symlinks here: {exc}")

    target = link / "state"
    assert not target.exists(), "the leaf must not exist; that is the branch the bug lived in"

    with pytest.raises(UnsafeStatePathError):
        state_root(tmp_path, str(target))


def test_state_root_resolves_a_path_that_does_not_exist_yet(tmp_path):
    target = tmp_path / "not" / "created" / "yet"
    assert not target.exists()
    result = state_root(tmp_path, str(target))
    assert result.is_dir()


def test_state_dir_pointing_at_a_file_is_a_named_error(tmp_path):
    target = tmp_path / "state"
    target.write_text("not a directory", encoding="utf-8")
    with pytest.raises(StateDirError) as caught:
        state_root(tmp_path, str(target))
    assert "existing file" in str(caught.value)
    assert "state_dir" in str(caught.value)


def test_state_dir_underneath_a_file_is_a_named_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(StateDirError) as caught:
        state_root(tmp_path, str(blocker / "deep" / "state"))
    message = str(caught.value)
    # The blocker is derived from the override, so it is elided with it. What
    # the message must still carry is which setting is wrong and why.
    assert "state_dir" in message
    assert "is a file, not a directory" in message
    assert "<elided>" in message


# A `state_dir` that resolves from `${env:...}` puts a live credential into every
# one of these messages. Redaction lives inside load_config, which knows what it
# resolved; paths.py does not, so it must echo nothing that came from the
# override. Each case below is a message that used to interpolate one.
_SECRET = "ghp_R3alSecretValue0000000000000000000000"


def _assert_chain_is_broken(exc):
    """`from exc` (or a bare `raise` inside an `except`) keeps the caught OSError
    reachable as `__cause__`/`__context__`, and `traceback.format_exception`,
    `logging.exception`, and the default excepthook all walk the chain and print
    it -- past whatever redaction the top-level message applied. The only
    rendering guaranteed clean is one where nothing is chained at all.
    """
    assert exc.__cause__ is None, exc.__cause__
    assert exc.__context__ is None, exc.__context__


def _assert_secret_is_unreachable(exc, secret):
    """The secret must not survive in any rendering of *exc*, including the chain."""
    rendered = "".join(traceback.format_exception(exc))
    assert secret not in str(exc), str(exc)
    assert secret not in repr(exc), repr(exc)
    assert all(secret not in str(arg) for arg in exc.args), exc.args
    assert secret not in rendered, rendered
    _assert_chain_is_broken(exc)


@pytest.mark.parametrize(
    "case",
    ["resolved-file", "resolved-under-file", "cloud-synced", "unset-variable"],
)
def test_no_error_message_echoes_the_state_dir_override(tmp_path, monkeypatch, case):
    if case == "resolved-file":
        target = tmp_path / _SECRET
        target.write_text("not a directory", encoding="utf-8")
        override = str(target)
    elif case == "resolved-under-file":
        blocker = tmp_path / _SECRET
        blocker.write_text("not a directory", encoding="utf-8")
        override = str(blocker / "deep" / "state")
    elif case == "cloud-synced":
        override = str(tmp_path / "OneDrive" / _SECRET)
    else:
        monkeypatch.delenv(_SECRET, raising=False)
        override = ("%" + _SECRET + "%\\w") if ON_WINDOWS else ("$" + _SECRET + "/w")

    with pytest.raises((StateDirError, UnsafeStatePathError, ConfigError)) as caught:
        state_root(tmp_path, override)
    message = str(caught.value)
    if case == "unset-variable":
        # This one names the VARIABLE on purpose -- a reference, not a value, and
        # the whole actionable content of the error. The chain must still be
        # broken; it just isn't the route being tested for secret absence here.
        assert message.count(_SECRET) == 2, message
        _assert_chain_is_broken(caught.value)
    else:
        assert _SECRET not in message, message
        assert "<elided>" in message
        _assert_secret_is_unreachable(caught.value, _SECRET)
    assert "state_dir" in message


def test_state_dir_as_an_existing_file_breaks_the_exception_chain(tmp_path):
    """Trigger 1: mkdir raises FileExistsError because the override names a file.

    This is the exact reproduction from the bug report: a `state_dir` override
    ending in a credential-shaped token, pointed at a path that already exists
    as a file.
    """
    target = tmp_path / _SECRET
    target.write_text("not a directory", encoding="utf-8")
    with pytest.raises(StateDirError) as caught:
        state_root(tmp_path, str(target))
    assert _SECRET not in str(caught.value)
    _assert_secret_is_unreachable(caught.value, _SECRET)


@windows_only
def test_state_dir_at_a_reserved_device_name_breaks_the_exception_chain(tmp_path):
    """Trigger 2: mkdir raises a *different* OSError subclass (FileNotFoundError,
    WinError 3) for an illegal path -- a reserved Windows device name as an
    intermediate component. Confirmed to leak the same way, by the same route,
    as the FileExistsError in trigger 1.
    """
    override = str(tmp_path / "NUL" / _SECRET)
    with pytest.raises(StateDirError) as caught:
        state_root(tmp_path, override)
    assert _SECRET not in str(caught.value)
    _assert_secret_is_unreachable(caught.value, _SECRET)


@posix_only
def test_state_dir_under_an_unwritable_directory_breaks_the_exception_chain(
    tmp_path,
):
    """POSIX sibling of the reserved-device-name trigger: PermissionError, a
    third OSError subclass, from a parent directory with no write permission.
    Skipped if the host lets root (or an ACL) bypass the permission bit.
    """
    blocker = tmp_path / "unwritable"
    blocker.mkdir()
    blocker.chmod(0o500)
    override = str(blocker / _SECRET)
    try:
        state_root(tmp_path, override)
    except StateDirError as caught:
        assert _SECRET not in str(caught)
        _assert_secret_is_unreachable(caught, _SECRET)
    else:
        pytest.skip("host does not enforce the permission bit (likely running as root)")
    finally:
        blocker.chmod(0o700)


def test_the_default_state_path_is_still_printed_in_full(tmp_path, monkeypatch):
    """Elision is for override-derived paths only; the default has no secret in it."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".whetstone").write_text("not a directory", encoding="utf-8")
    with pytest.raises(StateDirError) as caught:
        state_root(tmp_path / "proj")
    message = str(caught.value)
    assert str(tmp_path / ".whetstone") in message
    assert "<elided>" not in message


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_state_dir_does_not_silently_fall_back(tmp_path, empty):
    """A falsy override used to be ignored, relocating state without a word."""
    with pytest.raises(ConfigError, match="empty"):
        state_root(tmp_path, empty)


@posix_only
def test_unset_variable_in_state_dir_is_refused(tmp_path, monkeypatch):
    """loader._substitute errors on an unset ${env:VAR}; this must match."""
    monkeypatch.delenv("WHETSTONE_NOT_SET_ANYWHERE", raising=False)
    with pytest.raises(ConfigError, match="WHETSTONE_NOT_SET_ANYWHERE"):
        state_root(tmp_path, "$WHETSTONE_NOT_SET_ANYWHERE/w")


@windows_only
def test_unset_percent_variable_in_state_dir_is_refused(tmp_path, monkeypatch):
    """`%VAR%` is the spelling a Windows user writes, and ntpath leaves it literal."""
    monkeypatch.delenv("LOCALAPPDAT", raising=False)
    with pytest.raises(ConfigError, match="LOCALAPPDAT"):
        state_root(tmp_path, r"%LOCALAPPDAT%\whetstone")


@windows_only
def test_set_percent_variable_in_state_dir_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("WHETSTONE_TEST_BASE", str(tmp_path))
    result = state_root(tmp_path, r"%WHETSTONE_TEST_BASE%\w")
    assert result == (tmp_path / "w").resolve()
    assert result.is_dir()


@windows_only
def test_a_dollar_sign_is_a_legal_windows_directory_name(tmp_path):
    """`$Recycle.Bin` is a real Windows directory. Refusing it is the same bug."""
    target = tmp_path / "$Recycle.Bin" / "w"
    result = state_root(tmp_path, str(target))
    assert result.is_dir()


@posix_only
def test_a_percent_sign_is_a_legal_posix_directory_name(tmp_path):
    """`%` never expands on POSIX, so it is an ordinary filename character."""
    target = tmp_path / "%LOCALAPPDAT%" / "w"
    result = state_root(tmp_path, str(target))
    assert result.is_dir()


def test_set_variable_in_state_dir_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("WHETSTONE_TEST_BASE", str(tmp_path))
    result = state_root(tmp_path, "${WHETSTONE_TEST_BASE}/w")
    assert result == (tmp_path / "w").resolve()
    assert result.is_dir()


# Both branches of the platform guard, exercised on either host. The end-to-end
# tests above can only run on their own platform; these keep the other branch
# from rotting silently in CI.
@pytest.mark.parametrize(
    "windows,text,expected",
    [
        (False, "$XDG_STATE_HOME/w", "XDG_STATE_HOME"),
        (False, "${XDG_STATE_HOME}/w", "XDG_STATE_HOME"),
        (False, "/home/x/%LOCALAPPDAT%/w", None),
        (False, "/home/x/50%-off/w", None),
        (True, r"%LOCALAPPDAT%\w", "LOCALAPPDAT"),
        (True, r"%ProgramFiles(x86)%\w", "ProgramFiles(x86)"),
        (True, r"C:\Users\x\$Recycle.Bin\w", None),
        (True, r"C:\Users\x\$WINDOWS.~BT\w", None),
        (True, r"C:\Users\x\.whetstone\w", None),
    ],
)
def test_unexpanded_variable_detection_is_platform_specific(windows, text, expected):
    found = _unexpanded_variable(text, windows=windows)
    assert (found.group(1) if found else None) == expected


def test_relative_state_dir_resolves_against_the_project_not_the_cwd(
    tmp_path, monkeypatch
):
    """The config is found by walking up, so the CWD is not the project root."""
    project = tmp_path / "proj"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = state_root(project, ".whetstone-state")

    assert result == (project / ".whetstone-state").resolve()
    assert not (elsewhere / ".whetstone-state").exists()


# --- issue #3: capture_locals renders locals, and `root` held the secret ------
#
# `traceback.TracebackException(capture_locals=True)` renders every local with
# repr(). `paths.py`'s `root` is a Path built from the resolved `state_dir`, so
# a user running `rich`, `better-exceptions` or a Sentry-style reporter got the
# credential in their error output -- past every message-level elision, because
# the elision never reached a frame's locals.


def _locals_rendering(exc: BaseException) -> str:
    """Render *exc* the way rich / Sentry do, over the PRODUCTION frames only.

    `tb_next` drops the calling test's own frame. Without that, the test's
    `secret = "ghp_..."` local is itself rendered and every assertion below
    fails on the test harness rather than on the code under test.
    """
    tb = exc.__traceback__.tb_next if exc.__traceback__ else None
    return "".join(
        traceback.TracebackException(
            type(exc), exc, tb, capture_locals=True
        ).format()
    )


@pytest.mark.parametrize(
    "case", ["resolved-file", "resolved-under-file", "cloud-synced"]
)
def test_capture_locals_does_not_render_the_resolved_state_dir(tmp_path, case):
    if case == "resolved-file":
        target = tmp_path / _SECRET
        target.write_text("not a directory", encoding="utf-8")
        override = str(target)
    elif case == "resolved-under-file":
        blocker = tmp_path / _SECRET
        blocker.write_text("not a directory", encoding="utf-8")
        override = str(blocker / "deep" / "state")
    else:
        override = str(tmp_path / "OneDrive" / _SECRET)

    with pytest.raises((StateDirError, UnsafeStatePathError)) as caught:
        state_root(tmp_path, override)
    rendered = _locals_rendering(caught.value)
    # The population guard: capture_locals renders a `<locals>` block per frame,
    # and an empty rendering satisfies the absence assertion for free.
    assert "project_root =" in rendered, rendered
    assert _SECRET not in rendered, rendered


def test_the_default_state_path_is_still_rendered_in_locals(tmp_path, monkeypatch):
    """The counterweight. Scoping the override out of the frame must not strip
    the DEFAULT path too -- that one holds nothing the user did not already
    know, and eliding it makes an ordinary error unactionable."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    blocker = tmp_path / "home"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(StateDirError) as caught:
        state_root(tmp_path)
    assert "home" in _locals_rendering(caught.value)
