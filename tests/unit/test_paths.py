import subprocess
import sys
from pathlib import Path

import pytest

from whetstone.errors import ConfigError, StateDirError, UnsafeStatePathError
from whetstone.paths import assert_not_cloud_synced, state_root


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
    assert str(blocker) in str(caught.value)


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_state_dir_does_not_silently_fall_back(tmp_path, empty):
    """A falsy override used to be ignored, relocating state without a word."""
    with pytest.raises(ConfigError, match="empty"):
        state_root(tmp_path, empty)


def test_unset_variable_in_state_dir_is_refused(tmp_path, monkeypatch):
    """loader._substitute errors on an unset ${env:VAR}; this must match."""
    monkeypatch.delenv("WHETSTONE_NOT_SET_ANYWHERE", raising=False)
    with pytest.raises(ConfigError, match="WHETSTONE_NOT_SET_ANYWHERE"):
        state_root(tmp_path, "$WHETSTONE_NOT_SET_ANYWHERE/w")


def test_set_variable_in_state_dir_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("WHETSTONE_TEST_BASE", str(tmp_path))
    result = state_root(tmp_path, "${WHETSTONE_TEST_BASE}/w")
    assert result == (tmp_path / "w").resolve()
    assert result.is_dir()
