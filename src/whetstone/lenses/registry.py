"""Lens discovery: built-ins plus anything installed under the entry point."""

from __future__ import annotations

from importlib.metadata import entry_points

from ..errors import LensError
from .base import LensPack

ENTRY_POINT_GROUP = "whetstone.lenses"

_REGISTRY: dict[str, LensPack] = {}
_LOADED_PLUGINS = False


def register(pack: LensPack) -> None:
    if not isinstance(pack, LensPack):
        raise LensError(f"{pack!r} does not satisfy the LensPack protocol")
    _REGISTRY[pack.name] = pack


def _load_plugins() -> None:
    global _LOADED_PLUGINS
    if _LOADED_PLUGINS:
        return
    _LOADED_PLUGINS = True
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            register(entry.load()())
        except Exception as exc:  # noqa: BLE001 - a bad plugin must not kill the run
            raise LensError(f"lens plugin {entry.name!r} failed to load: {exc}") from exc


def get_lens(name: str) -> LensPack | None:
    _load_plugins()
    return _REGISTRY.get(name)


def available_lenses() -> list[str]:
    _load_plugins()
    return sorted(_REGISTRY)
