"""`_subprocess.kill_tree` -- the process-group branch, on every platform.

The POSIX branch does not exist on Windows, which is the platform this
project develops on, so it is reached here by patching `os.name` and the
three POSIX-only names it uses onto the real `os`/`signal` modules
(`_subprocess.os` IS `os`). Without that the guard below would be exercised
on the Linux CI leg only, and an invariant proved on one leg of four is one
a Windows-side change can break without anything going red locally.
"""

from __future__ import annotations

from whetstone import _subprocess


class _FakeProc:
    """Enough of Popen for `kill_tree`: a pid and a recording `kill`."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def _posix_kill_tree(monkeypatch, groups: dict[int, int]) -> list[int]:
    """Run `kill_tree`'s POSIX branch on any host, with *groups* as getpgid.

    `os.name`, `os.killpg`, `os.getpgid` and `signal.SIGKILL` are patched onto
    the real modules -- `_subprocess.os` IS `os` -- because the branch under
    test does not exist on Windows at all, and Windows is the platform this
    project develops on. Without this the guard would be verified on the Linux
    CI leg only, which is how an invariant becomes documentation.
    """
    killed: list[int] = []
    monkeypatch.setattr(_subprocess.os, "name", "posix")
    monkeypatch.setattr(_subprocess.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(_subprocess.os, "getpgid", groups.__getitem__, raising=False)
    monkeypatch.setattr(
        _subprocess.os,
        "killpg",
        lambda pgid, sig: killed.append(pgid),
        raising=False,
    )
    return killed


def test_kill_tree_never_signals_whetstones_own_process_group(monkeypatch):
    """`kill_tree` assumed every caller passed `**new_group()`. That assumption
    is documented, not enforced, and the helper is package-public. A child left
    in OUR group makes `os.killpg(os.getpgid(proc.pid), SIGKILL)` send SIGKILL
    to Whetstone itself -- mid-run, no traceback, no findings written."""
    # 4242 for both the child and us: the child never left our group.
    killed = _posix_kill_tree(monkeypatch, {1234: 4242, 0: 4242})
    proc = _FakeProc(pid=1234)

    _subprocess.kill_tree(proc)

    assert killed == [], "kill_tree signalled the group Whetstone is running in"
    assert proc.killed, "the direct-child kill must still run"


def test_kill_tree_still_kills_a_properly_isolated_group(monkeypatch):
    """The counterweight. Refusing our own group must not refuse every group --
    killing the tree is the whole point of this function."""
    killed = _posix_kill_tree(monkeypatch, {1234: 5555, 0: 4242})
    proc = _FakeProc(pid=1234)

    _subprocess.kill_tree(proc)

    assert killed == [5555]
    assert proc.killed
