"""Lens discovery: built-ins plus anything installed under the entry point."""

from __future__ import annotations

from importlib.metadata import entry_points

from ..errors import LensError
from .base import LensPack

ENTRY_POINT_GROUP = "whetstone.lenses"

_REGISTRY: dict[str, LensPack] = {}
_LOADED_PLUGINS = False
_LOAD_ERROR: LensError | None = None


def register(pack: LensPack) -> None:
    if not isinstance(pack, LensPack):
        raise LensError(f"{pack!r} does not satisfy the LensPack protocol")
    _REGISTRY[pack.name] = pack


def _load_plugins() -> None:
    """Load entry-point lenses once. A failure is remembered and re-raised.

    The flag alone is not enough: setting it before the loop turned one loud
    failure into a permanently silent partial registry, because later calls
    returned early with the broken plugin missing and no signal.
    """
    global _LOADED_PLUGINS, _LOAD_ERROR
    if _LOAD_ERROR is not None:
        raise _LOAD_ERROR
    if _LOADED_PLUGINS:
        return
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            register(entry.load()())
        except Exception as exc:  # noqa: BLE001 - any plugin failure must surface
            _LOAD_ERROR = LensError(f"lens plugin {entry.name!r} failed to load: {exc}")
            raise _LOAD_ERROR from exc
    _LOADED_PLUGINS = True


def get_lens(name: str) -> LensPack | None:
    _load_plugins()
    return _REGISTRY.get(name)


def available_lenses() -> list[str]:
    _load_plugins()
    return sorted(_REGISTRY)


def _register_builtins() -> None:
    from .code_defects.pack import CodeDefectsPack
    from .hygiene.pack import HygienePack

    register(HygienePack())
    # Registered UNCONFIGURED. It resolves its provider and its test command
    # from the run's config through `configure()`, which the runner calls; a
    # pack constructed here can hold nothing project-specific because the
    # registry is process-wide and one project's settings must not reach
    # another's run.
    register(CodeDefectsPack())


_register_builtins()
