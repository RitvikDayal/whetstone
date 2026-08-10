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
