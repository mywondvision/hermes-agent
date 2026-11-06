"""symphony_pm plugin — Hermes registry wiring for ``tools/symphony_pm_adapter.py``.

The adapter deliberately has no import-time registration; this plugin is its one explicit
binding to the Hermes tool registry, through the ``PluginContext`` the loader hands to
``register(ctx)``. At load the tool is registered UNWIRED (no port, no trusted-context source):
its ``check_fn`` is False, so ``get_tool_definitions`` never emits it, and a direct dispatch
returns ``UNAVAILABLE``/``hold``. Nothing here reads config, the home, the environment or the
network; nothing starts a thread or a process.

Wiring seam
-----------
A transport that owns a live ``SymphonyPmPort`` and can produce the out-of-band
``TrustedPmContextV1`` for the current request calls, in process, on the LOADED module
(``LoadedPlugin.module`` of the owning profile's ``PluginManager``, imported as
``hermes_plugins.symphony_pm``)::

    wire(port, context_source, trusted_issuers=(...), scheduler_id="...")

``wire()`` RE-REGISTERS the tool through the same ``ctx.register_tool`` path rather than
swapping a mutable slot behind a fixed handler, because that is how Hermes models "availability
changed": the registry generation bumps (invalidating the memoized tool definitions), the new
``check_fn`` is a new TTL-cache key (no stale False for 30 s), and the plugin ledger keeps every
generation so unload restores and finally removes them in order. The registered handler is the
adapter's own ``make_pm_tool_handler`` — no indirection that could be retargeted mid-call — so one
explicit request is still at most one port invocation, and the registry's ``check_fn`` is the
single truth for "a port and a context source are bound right now". ``unwire()`` registers the
inert generation again.

Authority never comes from tool arguments: ``context_source`` is the only way a trusted context
reaches the adapter, and it is supplied here, in process, by the trusted caller.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from tools.symphony_pm_adapter import (
    TOOL_NAME,
    TOOLSET,
    SymphonyPmAdapter,
    SymphonyPmPort,
    register_pm_tool,
)

__all__ = ["TOOLSET", "TOOL_NAME", "register", "unwire", "wire"]

_ctx: Any = None  # the PluginContext from register(); the only registry seam this module has


class _RegistryTarget:
    """The Hermes ``PluginContext`` seen through the adapter's ``target.register(**kwargs)`` shape.

    ``register_pm_tool`` passes ``name``, ``toolset``, ``schema``, ``handler``, ``check_fn`` and
    ``description`` — every one a keyword ``PluginContext.register_tool`` accepts, so the
    forwarding is verbatim. ``register_tool`` returns ``None`` when the host refused the
    registration (a global tool of that name, a registry rejection, an abandoned load); the
    handle is kept so callers fail closed instead of advertising a tool that is not there.
    """

    __slots__ = ("_ctx", "handle")

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self.handle = None

    def register(self, **kwargs: Any) -> None:
        self.handle = self._ctx.register_tool(**kwargs)


def _bind(adapter: Optional[SymphonyPmAdapter], context_source: Optional[Callable[[], Any]]) -> None:
    if _ctx is None:
        raise RuntimeError("symphony_pm plugin is not loaded; register(ctx) has not run")
    target = _RegistryTarget(_ctx)
    register_pm_tool(target, adapter=adapter, context_source=context_source)
    if target.handle is None:
        raise RuntimeError(f"Hermes refused to (re)register tool {TOOL_NAME!r} for plugin symphony_pm")


def register(ctx: Any) -> None:
    """Plugin entry point: register ``symphony_pm_ingress`` unwired (inert) in toolset ``symphony_pm``."""
    global _ctx
    _ctx = ctx
    _bind(None, None)


def wire(
    port: SymphonyPmPort,
    context_source: Callable[[], Any],
    *,
    trusted_issuers: tuple,
    scheduler_id: str,
) -> None:
    """Bind a live scheduler port and the trusted-context source; the tool becomes available.

    Called once by the in-process transport after the plugin has loaded (``register`` ran) and
    before it serves requests. Inputs are checked here so a misconfigured transport fails loudly
    at wiring time rather than advertising a tool whose every call is refused.
    """
    if port is None or not callable(context_source):
        raise ValueError("wire() needs a port object and a callable context_source")
    issuers = tuple(trusted_issuers)
    if not issuers or not all(isinstance(i, str) and i for i in issuers):
        raise ValueError("trusted_issuers must be a non-empty tuple of non-empty strings")
    if not isinstance(scheduler_id, str) or not scheduler_id:
        raise ValueError("scheduler_id must be a non-empty string")
    _bind(SymphonyPmAdapter(port, trusted_issuers=issuers, scheduler_id=scheduler_id), context_source)


def unwire() -> None:
    """Drop the bound port and context source; the tool is inert again (UNAVAILABLE, hidden)."""
    _bind(None, None)
