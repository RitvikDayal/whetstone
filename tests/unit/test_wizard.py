import io
import json
import sys
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from whetstone import doctor as doctor_module
from whetstone.config.loader import load_config
from whetstone.doctor import CheckResult
from whetstone.errors import UnsafeConfigTargetError
from whetstone.initialize.detect import Detection
from whetstone.initialize.wizard import build_config, render_config, run_wizard


def test_build_config_marks_unverified_commands_out(tmp_path):
    detection = Detection(
        languages=["python"],
        package_manager="uv",
        commands={"test": "uv run pytest", "lint": "uv run ruff check ."},
    )
    cfg = build_config(detection, name="demo", verified={"test": True, "lint": False})
    assert cfg.environment.commands.test == "uv run pytest"
    assert cfg.environment.commands.lint is None


def test_the_header_does_not_claim_dev_was_executed(tmp_path):
    """`dev` is recorded without being launched, so a blanket "every command
    below was executed and exited 0" is false for exactly that line -- and it
    was the line carrying `npm dev`, which could never have exited 0."""
    detection = Detection(
        languages=["javascript"], package_manager="npm", commands={"dev": "npm run dev"}
    )
    cfg = build_config(detection, name="demo", verified={"dev": True})
    text = render_config(cfg)

    assert "dev: npm run dev" in text
    header = text.split("version:")[0]
    assert "except `dev`" in header
    assert "never launched" in header


def test_rendered_config_is_valid_and_commented(tmp_path):
    detection = Detection(languages=["python"], package_manager="uv", commands={})
    cfg = build_config(detection, name="demo", verified={})
    text = render_config(cfg)
    assert text.lstrip().startswith("#")
    parsed = yaml.safe_load(text)
    assert parsed["project"]["name"] == "demo"
    assert parsed["lenses"]["hygiene"]["autonomy"] == 0


def _stub_run_command(monkeypatch, *, ok: bool = True):
    """Keep `run_wizard`'s real detection but stop it shelling out.

    These tests assert on config loading and on displayed evidence. Left
    unstubbed they really invoke `python -m pip install -e .`, `pytest` and
    `ruff check .` against a two-line pyproject in a temp directory: seconds of
    wall clock, an outcome that depends on what is installed on the host, and
    not one assertion that reads the result.
    """
    calls: list[tuple[str, str]] = []

    def _fake(label, command, cwd, timeout):
        calls.append((label, command))
        return CheckResult(name=f"command: {label}", ok=ok, detail="stub")

    monkeypatch.setattr("whetstone.initialize.wizard.run_command", _fake)
    return calls


def test_run_wizard_writes_a_loadable_config(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    _stub_run_command(monkeypatch)
    path = run_wizard(tmp_path, assume_yes=True)
    assert path.name == "whetstone.yaml"
    cfg = load_config(path)
    assert cfg.project.name == tmp_path.name


def test_wizard_refuses_to_overwrite_without_force(tmp_path):
    (tmp_path / "whetstone.yaml").write_text("version: 1\n", encoding="utf-8")
    try:
        run_wizard(tmp_path, assume_yes=True)
    except FileExistsError as exc:
        assert "--force" in str(exc)
    else:
        raise AssertionError("expected FileExistsError")


def test_verified_command_that_fails_is_not_written(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    detection = Detection(
        languages=["python"],
        package_manager="pip",
        commands={"test": f'"{sys.executable}" -c "import sys; sys.exit(1)"'},
    )
    cfg = build_config(detection, name="demo", verified={"test": False})
    assert cfg.environment.commands.test is None


def test_wizard_tells_the_user_about_unused_polyglot_commands(tmp_path, monkeypatch):
    # Real polyglot fixture, not a stub -- both languages actually present on
    # disk, exactly like the detect_stack fixture this mirrors. Only the
    # EXECUTION is stubbed; detection still reads the files above.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    _stub_run_command(monkeypatch)

    console = Console(file=io.StringIO(), width=300)
    run_wizard(tmp_path, console=console, assume_yes=True)

    out = console.file.getvalue()
    assert "pnpm run test" in out
    assert "not used" in out


def test_wizard_says_what_it_is_about_to_execute(tmp_path, monkeypatch):
    """`init --yes` runs the project's own commands in a repo the user may have
    cloned minutes ago and not read. shell=True with cwd=project_root means
    cmd.exe resolves from the working directory before PATH on a default
    Windows box, so `npm install` can be a `npm.bat` sitting in the clone.

    Nothing here can stop that -- running the commands IS what init does. What
    it can do is not be quiet about it: name the commands before running them,
    and name the directory they run in.
    """
    detection = Detection(
        languages=["javascript"],
        package_manager="npm",
        commands={"install": "npm install", "test": "npm run test"},
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)
    monkeypatch.setattr(
        "whetstone.initialize.wizard.run_command",
        lambda label, command, cwd, timeout: CheckResult(
            name=f"command: {label}", ok=True, detail="stub"
        ),
    )

    # An explicit wide Console, not capsys: rich wraps to the terminal width and
    # a wrapped temp path would make this assert about the wrap, not the notice.
    console = Console(file=io.StringIO(), width=300)
    run_wizard(tmp_path, console=console, assume_yes=True)

    out = console.file.getvalue()
    assert "npm install" in out
    assert "npm run test" in out
    # The directory is half the warning: what runs depends on where it runs.
    assert str(tmp_path) in out
    assert "about to execute" in out.lower()


def test_wizard_says_nothing_scary_when_there_is_nothing_to_run(tmp_path, monkeypatch):
    """A warning that appears when no command will run is noise, and noise is
    how a warning stops being read."""
    monkeypatch.setattr(
        "whetstone.initialize.wizard.detect_stack",
        lambda root: Detection(languages=[], package_manager=None, commands={}),
    )
    console = Console(file=io.StringIO(), width=200)
    run_wizard(tmp_path, console=console, assume_yes=True)
    assert "about to execute" not in console.file.getvalue().lower()


def test_wizard_uses_doctors_per_label_timeouts(tmp_path, monkeypatch):
    detection = Detection(
        languages=["python"],
        package_manager="pip",
        # The spellings `detect_stack` actually emits. `pip install -e .` was a
        # fixture-only shape: detect.py emits `python -m pip install -e .` on
        # purpose, because a bare `pip` on PATH is frequently a shim that eats
        # the exit code. A fixture carrying the banned spelling is the shape
        # that hides a broken detection-to-wizard contract.
        commands={
            "install": "python -m pip install -e .",
            "test": "pytest",
            "lint": "ruff check .",
            "build": "python -m build",
        },
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)

    seen_timeouts: dict[str, int] = {}

    def _fake_run_command(label, command, cwd, timeout):
        seen_timeouts[label] = timeout
        return CheckResult(name=f"command: {label}", ok=True, detail="stub")

    monkeypatch.setattr("whetstone.initialize.wizard.run_command", _fake_run_command)
    run_wizard(tmp_path, assume_yes=True)

    # Every label's timeout must be the exact value doctor.py uses for the
    # same label -- imported, not copied, so the two cannot drift apart.
    assert seen_timeouts == doctor_module._TIMEOUTS


def test_wizard_confirmed_command_actually_runs(tmp_path, monkeypatch):
    detection = Detection(
        languages=["python"], package_manager="pip", commands={"test": "pytest"}
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))

    def _fake_run_command(label, command, cwd, timeout):
        return CheckResult(name=f"command: {label}", ok=True, detail="stub")

    monkeypatch.setattr("whetstone.initialize.wizard.run_command", _fake_run_command)
    path = run_wizard(tmp_path, assume_yes=False)

    cfg = load_config(path)
    assert cfg.environment.commands.test == "pytest"


def test_wizard_declined_command_never_runs(tmp_path, monkeypatch):
    detection = Detection(
        languages=["python"], package_manager="pip", commands={"test": "pytest"}
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))

    def _fail_if_called(label, command, cwd, timeout):
        raise AssertionError("a declined command must never run")

    monkeypatch.setattr("whetstone.initialize.wizard.run_command", _fail_if_called)
    path = run_wizard(tmp_path, assume_yes=False)

    cfg = load_config(path)
    assert cfg.environment.commands.test is None


def test_wizard_records_dev_without_launching_it(tmp_path, monkeypatch):
    # `pnpm run dev`, not `pnpm dev`: detect.py routes every script through
    # `run` for every manager, and the bare-verb form is exactly the one that
    # produced the `npm dev` defect this branch fixed. The fixture must not
    # re-encode it.
    detection = Detection(
        languages=["javascript"],
        package_manager="pnpm",
        commands={"dev": "pnpm run dev"},
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)

    def _fail_if_called(label, command, cwd, timeout):
        raise AssertionError("dev must never be launched by the wizard")

    monkeypatch.setattr("whetstone.initialize.wizard.run_command", _fail_if_called)
    path = run_wizard(tmp_path, assume_yes=True)

    cfg = load_config(path)
    assert cfg.environment.commands.dev == "pnpm run dev"


def test_wizard_survives_closed_stdin(tmp_path, monkeypatch):
    detection = Detection(
        languages=["python"],
        package_manager="pip",
        commands={"test": "pytest", "lint": "ruff check ."},
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # EOF on the very first read

    def _fail_if_called(label, command, cwd, timeout):
        raise AssertionError("a command must not run when stdin cannot be read")

    monkeypatch.setattr("whetstone.initialize.wizard.run_command", _fail_if_called)
    # Must not raise EOFError.
    path = run_wizard(tmp_path, assume_yes=False)

    cfg = load_config(path)
    assert cfg.environment.commands.test is None
    assert cfg.environment.commands.lint is None


# --- every recorded reason reaches the user ----------------------------------


def test_every_recorded_detection_reason_is_displayed(tmp_path, monkeypatch):
    """`_print_detection` used to render only `package_manager` and keys ending
    `_commands_unused`. Detection records more than that, and the rest were
    written down and never shown -- an omission with a reason attached that the
    user never reads is the same silent omission, one layer out.

    The evidence key here is deliberately one no detector emits: the rendering
    must be driven by exclusion, not by a list of known keys that a future
    detect.py change would have to remember to extend.
    """
    detection = Detection(
        languages=["python"],
        package_manager="pip",
        commands={},
        evidence={
            "python": "found pyproject.toml",
            "package_manager": "no Python lockfile found",
            "some_future_reason": "a reason no wizard code knows about",
        },
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)
    console = Console(file=io.StringIO(), width=300)
    run_wizard(tmp_path, console=console, assume_yes=True)

    assert "a reason no wizard code knows about" in console.file.getvalue()


def test_the_setup_cfg_install_omission_reaches_the_user(tmp_path, monkeypatch):
    """Real fixture, real detection. setup.cfg alone means no install command is
    proposed; detect.py records why and the wizard has to say it."""
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = x\n", encoding="utf-8")
    _stub_run_command(monkeypatch)

    console = Console(file=io.StringIO(), width=300)
    run_wizard(tmp_path, console=console, assume_yes=True)

    out = console.file.getvalue()
    assert "only setup.cfg was found" in out
    assert "no install command is proposed" in out


def test_an_unreadable_scripts_table_reaches_the_user(tmp_path, monkeypatch):
    """`{"scripts": null}` is a shape real tooling emits. Detection coerces it to
    empty and records that the table could not be read; without this the user
    sees a Node project with no scripts and no way to tell that apart from a
    project that declares none."""
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": None}), encoding="utf-8"
    )
    _stub_run_command(monkeypatch)

    console = Console(file=io.StringIO(), width=300)
    run_wizard(tmp_path, console=console, assume_yes=True)

    out = console.file.getvalue()
    assert "scripts could not be read" in out
    assert "not the same as declaring none" in out


def test_conventional_proposals_are_not_described_as_the_projects_own(
    tmp_path, monkeypatch
):
    """Every command row read "from project manifest", and the announcement said
    all of them came from it. A pyproject holding only `[build-system]` declares
    no test command at all; `pytest` there is this tool's guess, and the user is
    being asked to let it run."""
    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    _stub_run_command(monkeypatch)

    console = Console(file=io.StringIO(), width=300)
    run_wizard(tmp_path, console=console, assume_yes=True)

    out = console.file.getvalue()
    assert "conventional" in out
    assert "from project manifest" not in out


# --- the config target is a real file, or nothing is written -----------------


def _dangling_symlink(link: Path, target: Path) -> None:
    """Point *link* at a *target* that does not exist, or skip.

    No junction fallback here, unlike `tests/unit/test_scope.py::_link_out`: a
    junction is directory-only and whetstone.yaml is a file, so there is no
    unprivileged Windows equivalent to fall back to. The skip is therefore
    real, and it is why `test_a_symlinked_target_is_refused_on_every_platform`
    below exists -- that one runs everywhere and asserts the same refusal.
    """
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform
        pytest.skip(f"cannot create a symlink here: {exc}")


def test_a_dangling_symlinked_config_does_not_write_outside_the_project(tmp_path):
    """`Path.exists()` FOLLOWS the link, so a dangling one reads as "no file
    here": the --force guard never fires, and `write_text` then creates the
    external target. git carries symlinks in tree objects, so a committed
    `whetstone.yaml -> ../outside/created.yaml` turns the first command a new
    user runs into a write outside the worktree.
    """
    project_root = tmp_path / "project"
    outside = tmp_path / "outside"
    project_root.mkdir()
    outside.mkdir()
    escape = outside / "created.yaml"
    _dangling_symlink(project_root / "whetstone.yaml", escape)
    assert not escape.exists(), "the target must not exist; that is the branch"

    with pytest.raises(UnsafeConfigTargetError) as caught:
        run_wizard(project_root, assume_yes=True)

    assert "symlink" in str(caught.value)
    assert not escape.exists(), "init wrote through the link and left the worktree"


def test_force_does_not_authorise_writing_through_a_symlink(tmp_path):
    """--force means "overwrite the config I already have", not "follow whatever
    indirection the repository put in its place"."""
    project_root = tmp_path / "project"
    outside = tmp_path / "outside"
    project_root.mkdir()
    outside.mkdir()
    escape = outside / "existing.yaml"
    escape.write_text("untouched\n", encoding="utf-8")
    _dangling_symlink(project_root / "whetstone.yaml", escape)

    with pytest.raises(UnsafeConfigTargetError):
        run_wizard(project_root, assume_yes=True, force=True)

    assert escape.read_text(encoding="utf-8") == "untouched\n"


def test_a_symlinked_target_is_refused_on_every_platform(tmp_path, monkeypatch):
    """The two tests above need a privilege an unelevated Windows user does not
    have, so on that platform they skip -- and Windows is where this tool is
    used most. This one fakes only the lstat answer and asserts what the wizard
    does with it: refuse, before running anything, having written nothing.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: self.name == "whetstone.yaml" or real_is_symlink(self),
    )

    def _fail_if_called(label, command, cwd, timeout):
        raise AssertionError("nothing may run before the target is known to be safe")

    monkeypatch.setattr("whetstone.initialize.wizard.run_command", _fail_if_called)

    with pytest.raises(UnsafeConfigTargetError):
        run_wizard(tmp_path, assume_yes=True)

    assert not (tmp_path / "whetstone.yaml").exists()
