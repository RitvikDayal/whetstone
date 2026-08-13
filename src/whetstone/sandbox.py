"""An OS-enforced boundary for running code Whetstone did not write.

WHY THIS EXISTS. The reproduce stage asks a model for a pytest file and then
runs it. `kind: "pytest"` sounded like a restriction and is not one -- a pytest
file is an arbitrary Python program, and it can write anywhere the process can
write, reach into `.git`, or spawn anything at all. The policy gate bounds the
provider stage; it bounds nothing about the artifact. The worktree sentinel
looks only afterwards, and only inside `project_root`.

So the boundary is the operating system's, not ours:

- `--network none`. This is the one that matters most. A container with no
  network cannot push, cannot deploy, and cannot send the repository anywhere,
  and none of that depends on us enumerating commands to forbid.
- The worktree is the ONLY mount. A write outside it is not detected after the
  fact, it is impossible.
- `--cap-drop ALL`, no privileges, a pid cap and a memory cap, so a runaway
  artifact takes the container down rather than the machine.

FAIL CLOSED, ALWAYS. No configured image, no Docker, or a daemon that will not
answer all produce the same outcome: nothing runs, the reason travels back, and
the finding is capped because its evidence was never executed. There is
deliberately no default image -- an upgrade must never be the thing that grants
arbitrary code execution.

WHAT IT DOES NOT CLOSE. The artifact still reads and writes the worktree, which
is the point of running it. A container is not a security boundary against a
determined kernel exploit. And the image is the user's: Whetstone cannot know
whether it is safe, only that it is the one they named.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ._subprocess import ShellResult, run_shell

# Where the worktree appears inside the container. Fixed rather than derived:
# the command the user declared has to be runnable, and a path that changes per
# run is one more thing that can differ between a passing and a failing stage.
CONTAINER_WORKDIR = "/whetstone"

# Bounds a runaway artifact rather than the machine. Generous enough that an
# ordinary test suite is unaffected.
_PIDS_LIMIT = 512
_MEMORY = "2g"

_DOCKER = "docker"


@dataclass(frozen=True)
class SandboxUnavailable:
    """Why nothing can be executed. Carries a sentence for the user."""

    reason: str


def _docker_present() -> str | None:
    if shutil.which(_DOCKER) is None:
        return "docker is not installed or not on PATH"
    return None


def availability(image: str | None) -> SandboxUnavailable | None:
    """None when an artifact could be executed, or why it cannot be.

    Checked BEFORE anything is written or run, so the caller can refuse without
    side effects -- and so the reason is the same whether the refusal happens
    on a developer's laptop or on a CI leg with no Docker.
    """
    if not image or not image.strip():
        return SandboxUnavailable(
            "no sandbox image is configured, and Whetstone does not execute "
            "model-written code outside one. Set a sandbox image to enable "
            "reproduction; without it a finding cannot be graded above B."
        )
    missing = _docker_present()
    if missing:
        return SandboxUnavailable(
            f"{missing}, so there is no boundary to run the reproduction inside. "
            f"The finding cannot be graded above B."
        )
    probe = run_shell(f"{_DOCKER} info", Path.cwd(), timeout=30)
    if probe.returncode != 0:
        detail = " ".join((probe.stderr or probe.stdout or "").split())[:160]
        return SandboxUnavailable(
            f"the docker daemon did not answer ({detail or 'no output'}), so there "
            f"is no boundary to run the reproduction inside."
        )
    return None


def docker_argv(command: str, worktree: Path, image: str) -> list[str]:
    """The exact argv, exposed so a test can assert the bound rather than infer it.

    Every flag here is load-bearing and none is decoration:

    `--network none` is why a reproduction cannot push, deploy or exfiltrate.
    `--rm` is why a failed run leaves nothing behind. The single `-v` is why a
    write outside the worktree is impossible rather than merely noticed. The
    rest bound a runaway rather than the machine.
    """
    return [
        _DOCKER,
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(_PIDS_LIMIT),
        "--memory",
        _MEMORY,
        "-v",
        f"{worktree}:{CONTAINER_WORKDIR}",
        "-w",
        CONTAINER_WORKDIR,
        image,
        "sh",
        "-lc",
        command,
    ]


def run_sandboxed(
    command: str, worktree: Path, image: str, timeout: int
) -> ShellResult:
    """Run *command* against *worktree* inside a container, bounded.

    Assumes `availability()` already said yes. Split that way so the caller can
    refuse before writing an artifact it will not run.
    """
    argv = docker_argv(command, worktree, image)
    quoted = " ".join(f'"{part}"' if " " in part else part for part in argv)
    return run_shell(quoted, worktree, timeout)
