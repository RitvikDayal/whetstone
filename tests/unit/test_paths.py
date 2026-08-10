import subprocess
import sys
from pathlib import Path

import pytest

from whetstone.errors import UnsafeStatePathError
from whetstone.paths import assert_not_cloud_synced, state_root


@pytest.mark.parametrize(
    "bad",
    [
        r"C:\Users\x\OneDrive\whetstone",
        r"C:\Users\x\Dropbox\state",
        "/Users/x/Library/CloudStorage/GoogleDrive-a/state",
        "/home/x/Google Drive/state",
    ],
)
def test_cloud_synced_paths_are_refused(bad):
    with pytest.raises(UnsafeStatePathError):
        assert_not_cloud_synced(Path(bad))


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
