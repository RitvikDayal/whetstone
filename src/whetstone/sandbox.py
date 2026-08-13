"""An OS-enforced boundary for running code Whetstone did not write.

WHY THIS EXISTS. The reproduce stage asks a model for a pytest file and then
runs it. `kind: "pytest"` sounded like a restriction and is not one -- a pytest
file is an arbitrary Python program, and it can write anywhere the process can
write, reach into `.git`, or spawn anything at all. The policy gate bounds the
provider stage; it bounds nothing about the artifact. The worktree sentinel
looks only afterwards, and only inside `project_root`.

So the boundary is the operating system's:

- `--network none`. The one that matters most. A container with no network
  cannot push, cannot deploy, and cannot send the repository anywhere, and none
  of that depends on us enumerating commands to forbid.
- The worktree is the ONLY mount. A write outside it is not detected after the
  fact, it is impossible.
- `--cap-drop ALL`, no new privileges, pid/memory caps, and the host's own
  uid/gid where there is one, so the container cannot leave root-owned files in
  somebody's repository.

FAIL CLOSED, ALWAYS. No configured image, no Docker, a daemon that will not
answer, or a daemon not running Linux containers all produce the same outcome:
nothing runs, the reason travels back, and the finding is capped because its
evidence was never executed. There is deliberately no default image -- an
upgrade must never be the thing that grants arbitrary code execution.

NO HOST SHELL. The argv goes to `run_argv`, not to a shell. Flattening it into
a string means the host shell re-parses every quote inside the command, which
broke differently on the Windows and Ubuntu CI legs -- the shape of every
quoting defect: correct on the machine it was written on.

WHAT IT DOES NOT CLOSE. The artifact still reads and writes the worktree, which
is the point of running it. A container is not a boundary against a kernel
exploit. And the image is the user's: Whetstone knows it is the one they named
and nothing else about it.
"""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from ._subprocess import ShellResult, run_argv

# Where the worktree appears inside the container. Fixed rather than derived:
# the command the user declared has to be runnable, and a path that changes per
# run is one more thing that can differ between a passing and a failing stage.
CONTAINER_WORKDIR = "/whetstone"

# Bounds a runaway artifact rather than the machine.
_PIDS_LIMIT = 512
_MEMORY = "2g"

_DOCKER = "docker"
_PROBE_TIMEOUT = 30
_STOP_TIMEOUT = 30

# `docker info` is a daemon round trip and the answer cannot change during a
# run. `reproduce` asks once per candidate, so without this a fifty-candidate
# run pays fifty round trips to learn the same thing.
_probe_cache: tuple[bool, str] | None = None


@dataclass(frozen=True)
class SandboxUnavailable:
    """Why nothing can be executed. Carries a sentence for the user."""

    reason: str


def reset_probe_cache() -> None:
    """Forget the cached daemon answer. For tests, which change the world."""
    global _probe_cache
    _probe_cache = None


def _probe_daemon() -> tuple[bool, str]:
    """`(usable, reason)` for the local daemon, cached for the process."""
    global _probe_cache
    if _probe_cache is not None:
        return _probe_cache

    result = run_argv(
        [_DOCKER, "info", "--format", "{{.OSType}}"], Path.cwd(), _PROBE_TIMEOUT
    )
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "").split())[:160]
        answer = (
            False,
            f"the docker daemon did not answer ({detail or 'no output'}), so there "
            f"is no boundary to run the reproduction inside.",
        )
    else:
        os_type = result.stdout.strip().lower()
        if os_type != "linux":
            # Measured on the windows-latest CI legs: the daemon answers, and
            # then `docker run` fails with "Windows does not support PidsLimit".
            # A daemon that cannot run our containers is an unavailable
            # sandbox, and saying so here is the difference between a refusal
            # and a stage that dies at exit 125.
            answer = (
                False,
                f"the docker daemon is running {os_type or 'unknown'} containers "
                f"rather than linux ones, so the reproduction cannot be isolated.",
            )
        else:
            answer = (True, "")
    _probe_cache = answer
    return answer


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
    if shutil.which(_DOCKER) is None:
        return SandboxUnavailable(
            "docker is not installed or not on PATH, so there is no boundary to "
            "run the reproduction inside. The finding cannot be graded above B."
        )
    usable, reason = _probe_daemon()
    return None if usable else SandboxUnavailable(reason)


def _user_flags() -> list[str]:
    """Run as the invoking user where the platform has one.

    Without this the container runs as root and every file it leaves in the
    mounted worktree -- `__pycache__`, a stray report -- is owned by root in
    somebody's own repository.
    """
    if not hasattr(os, "getuid"):
        return []
    return ["--user", f"{os.getuid()}:{os.getgid()}"]


def docker_argv(
    command: str, worktree: Path, image: str, name: str, env: dict[str, str]
) -> list[str]:
    """The exact argv, exposed so a test can assert the bound rather than infer it.

    Every flag is load-bearing. `--network none` is why a reproduction cannot
    push, deploy or exfiltrate. The single `-v` is why a write outside the
    worktree is impossible rather than merely noticed. `--name` is what lets a
    timed-out container be stopped rather than left running against the mount.
    """
    argv = [
        _DOCKER,
        "run",
        "--rm",
        "--name",
        name,
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
        # Without this, `--memory` caps RAM and leaves swap unbounded.
        "--memory-swap",
        _MEMORY,
        *_user_flags(),
    ]
    for key, value in sorted(env.items()):
        # Through docker rather than as a `VAR=value` command prefix: a prefix
        # binds to one simple command, so `cd sub && pytest` would apply it to
        # `cd` alone and pytest would write bytecode into the mounted worktree.
        argv += ["-e", f"{key}={value}"]
    argv += [
        "-v",
        f"{worktree}:{CONTAINER_WORKDIR}",
        "-w",
        CONTAINER_WORKDIR,
        image,
        "sh",
        "-lc",
        command,
    ]
    return argv


def run_sandboxed(
    command: str,
    worktree: Path,
    image: str,
    timeout: int,
    env: dict[str, str] | None = None,
) -> ShellResult:
    """Run *command* against *worktree* inside a container, bounded.

    Assumes `availability()` already said yes. Split that way so the caller can
    refuse before writing an artifact it will not run.
    """
    name = f"whetstone-repro-{uuid.uuid4().hex[:12]}"
    argv = docker_argv(command, worktree, image, name, env or {})
    result = run_argv(argv, worktree, timeout)
    if result.timed_out:
        # Killing the docker CLIENT does not stop the container. It keeps
        # running against the mount, so without this the sentinel races writes
        # that arrive after the stage was declared timed out.
        run_argv([_DOCKER, "rm", "-f", name], worktree, _STOP_TIMEOUT)
    return result
