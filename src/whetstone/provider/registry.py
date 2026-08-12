"""Provider discovery: the built-in CLI provider plus anything installed.

Mirrors `lenses/registry.py`, including the part that was paid for: the loaded
flag is set AFTER the loop and a failure is remembered, because setting it
first turned one loud plugin failure into a permanently silent partial
registry, where every later call returned early with that plugin missing and
nothing saying so.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from .base import Provider, ProviderError

ENTRY_POINT_GROUP = "whetstone.providers"

_REGISTRY: dict[str, Provider] = {}
_LOADED_PLUGINS = False
_LOAD_ERROR: ProviderError | None = None


def register(provider: Provider) -> None:
    if not isinstance(provider, Provider):
        raise ProviderError(f"{provider!r} does not satisfy the Provider protocol")
    _REGISTRY[provider.name] = provider


def _load_plugins() -> None:
    global _LOADED_PLUGINS, _LOAD_ERROR
    if _LOAD_ERROR is not None:
        raise _LOAD_ERROR
    if _LOADED_PLUGINS:
        return
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            register(entry.load()())
        except Exception as exc:  # noqa: BLE001 - any plugin failure must surface
            _LOAD_ERROR = ProviderError(
                f"provider plugin {entry.name!r} failed to load: {exc}"
            )
            raise _LOAD_ERROR from exc
    _LOADED_PLUGINS = True


def get_provider(name: str) -> Provider:
    """The named provider, or an error naming it.

    Refuses rather than returning None: a caller that forgets the None check
    reaches the model layer with nothing, and the failure surfaces somewhere
    that cannot say which provider was asked for.
    """
    _load_plugins()
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ProviderError(
            f"no provider named {name!r}. "
            f"Available: {', '.join(sorted(_REGISTRY))}."
        ) from exc


def available_providers() -> list[str]:
    _load_plugins()
    return sorted(_REGISTRY)


def _register_builtins() -> None:
    from .claude_cli import ClaudeCliProvider

    register(ClaudeCliProvider())


_register_builtins()
