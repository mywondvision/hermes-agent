"""Declarative plugin services, started explicitly by the owning serving host.

Discovery allocates only declarations. There are no threads, processes, journals,
config changes or network calls in this module. Service factories own their cleanup
and must respect the monotonic shutdown deadline; the outer host watchdog remains
the bound for a plugin callback that does not return.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
import threading
import time
from types import MappingProxyType
from typing import Any, Callable
import uuid

from hermes_cli.plugin_request_binding import canonical_home

_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
logger = logging.getLogger('hermes_cli.plugins')


@dataclass
class _Lifetime:
    active: bool = False
    closing: bool = False
    callbacks: list = field(default_factory=list)
    failed_cleanup: list = field(default_factory=list)


@dataclass(frozen=True)
class ServiceScope:
    profile_home: str
    namespace: str
    generation: str
    binding: Any
    _lifetime: _Lifetime = field(default_factory=_Lifetime, repr=False, compare=False)

    @property
    def active(self) -> bool:
        return self._lifetime.active and not self._lifetime.closing

    def on_close(self, callback: Callable[[float], None]) -> None:
        """Register cleanup before publishing a resource; callback gets a monotonic deadline."""
        if not callable(callback) or self._lifetime.closing:
            raise RuntimeError('service cleanup registration refused')
        self._lifetime.callbacks.append(callback)

    def _close_admission(self):
        self._lifetime.closing = True
        self._lifetime.active = False

    def _close(self, deadline):
        from pathlib import Path
        from hermes_cli.plugins_loader import _plugin_home_scope
        self._close_admission()
        while self._lifetime.callbacks:
            callback = self._lifetime.callbacks.pop()
            try:
                with _plugin_home_scope(Path(self.profile_home)):
                    callback(deadline)
            except (Exception, SystemExit) as exc:  # health: allow BLE001 -- continue cleanup of independently owned resources
                self._lifetime.failed_cleanup.append((callback, type(exc).__name__))
        return tuple(name for _, name in self._lifetime.failed_cleanup)


@dataclass
class _Entry:
    ctx: Any
    name: str
    factory: Callable
    registration: Any = None
    scope: ServiceScope | None = None
    value: Any = None
    attempted: bool = False
    failure: str = ''


class ServiceReference:
    """One declaration generation. Unload cannot retarget an old reference to its replacement."""
    def __init__(self, entry):
        self._entry = entry

    def get(self):
        entry = self._entry
        if (entry.registration is None or not entry.registration.active
                or entry.scope is None or not entry.scope.active):
            return None
        return entry.value


class _Catalog:
    def __init__(self, home):
        self.home = canonical_home(home)
        self.entries = {}
        self.lock = threading.RLock()
        self.owner = None

    def remove(self, key, entry):
        with self.lock:
            if self.entries.get(key) is not entry:
                return
            if entry.scope is not None:
                failures = entry.scope._close(time.monotonic())
                if failures:
                    logger.warning('Runtime service %s cleanup incomplete (%s)', ':'.join(key), ','.join(failures))
                    return  # retain exact resource callbacks and block a replacement owner
            self.entries.pop(key)

    def start(self, owner, bindings):
        with self.lock:
            if self.owner is not None and self.owner is not owner:
                raise RuntimeError('profile services already belong to another host')
            self.owner = owner
            for key, entry in tuple(self.entries.items()):
                if entry.attempted or not entry.registration.active:
                    continue
                entry.attempted = True  # startup exceptions never imply a blind retry
                scope = ServiceScope(self.home, ':'.join(key), uuid.uuid4().hex, bindings.get(key))
                entry.scope = scope
                try:
                    from hermes_cli.plugins_loader import _plugin_home_scope
                    with _plugin_home_scope(entry.ctx._manager.home_path):
                        value = entry.factory(scope)
                    if entry.ctx._load_abandoned or not entry.registration.active:
                        raise RuntimeError('service declaration abandoned')
                    entry.value = value
                    scope._lifetime.active = True
                except (Exception, SystemExit) as exc:  # health: allow BLE001 -- arbitrary factory failures close only this consumer
                    entry.failure = type(exc).__name__
                    scope._close(time.monotonic())
                    logger.warning('Runtime service %s failed (%s); admission closed', ':'.join(key), entry.failure)

    def close_admission(self, owner):
        with self.lock:
            if self.owner is not owner:
                return
            for entry in self.entries.values():
                if entry.scope is not None:
                    entry.scope._close_admission()

    def stop(self, owner, deadline):
        with self.lock:
            if self.owner is not owner:
                return ()
            self.close_admission(owner)
            failures = []
            for entry in reversed(tuple(self.entries.values())):
                if entry.scope is not None:
                    failures.extend(entry.scope._close(deadline))
            # Spent declaration generations cannot be restarted by another host.
            return tuple(failures)


def _catalog(manager):
    # register() runs on a deadline worker while its parent holds _discovery_lock.
    # A catalog is only Python bookkeeping; setdefault publishes one without
    # making that worker wait for its own loading parent.
    return vars(manager).setdefault('_runtime_service_catalog', _Catalog(manager.home_path))


class PluginServiceContextMixin:
    def register_runtime_service(self, name: str, factory: Callable):
        """Declare a factory; only an owning serving host may start it after discovery."""
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not callable(factory):
            raise ValueError('invalid runtime service declaration')
        catalog = _catalog(self._manager)
        key = (self.plugin_id, name)
        with catalog.lock:
            if self._load_abandoned:
                return None
            if key in catalog.entries:
                raise ValueError('duplicate runtime service declaration')
            entry = _Entry(self, name, factory)
            entry.registration = self._track('runtime_service', name, lambda: catalog.remove(key, entry))
            catalog.entries[key] = entry
            if self._load_abandoned:
                entry.registration.dispose()
                return None
            return ServiceReference(entry)


def close_plugin_service_admission(manager, plugin_keys):
    """Unload closes service generations before it disposes any registered tool."""
    catalog = getattr(manager, '_runtime_service_catalog', None)
    if catalog is None:
        return
    with catalog.lock:
        for (plugin_key, _), entry in catalog.entries.items():
            if plugin_key in plugin_keys and entry.scope is not None:
                entry.scope._close_admission()


class ServiceHost:
    """Host-owned group of profile services; not a dispatcher or an execution runtime.

    Bindings are explicit in-process host objects, never imported from config/tool
    args. The production approval source is deliberately not supplied by default.
    """
    def __init__(self):
        self._owner = object()
        self._profiles = {}
        self._bindings = {}
        self._closing = False
        self._lock = threading.RLock()

    def configure(self, home, bindings):
        key = canonical_home(home)
        with self._lock:
            if self._closing or key in self._profiles:
                raise RuntimeError('service bindings are frozen after profile start')
            self._bindings[key] = MappingProxyType(dict(bindings))

    def start_profile(self, home):
        from hermes_cli.plugins import discover_plugins, get_plugin_manager
        from hermes_cli.plugins_loader import _plugin_home_scope
        key = canonical_home(home)
        with self._lock, _plugin_home_scope(home):
            if self._closing:
                raise RuntimeError('service host is closing')
            discover_plugins()
            manager = get_plugin_manager()
            catalog = _catalog(manager)
            self._profiles[key] = catalog
            with manager._discovery_lock:
                catalog.start(self._owner, self._bindings.get(key, {}))

    def close_admission(self):
        with self._lock:
            self._closing = True
            for catalog in self._profiles.values():
                catalog.close_admission(self._owner)

    def stop(self, *, timeout=2.0):
        deadline = time.monotonic() + max(0.0, timeout)
        self.close_admission()
        failures = []
        with self._lock:
            for catalog in reversed(tuple(self._profiles.values())):
                failures.extend(catalog.stop(self._owner, deadline))
        if failures:
            logger.warning('Runtime service cleanup incomplete (%s)', ','.join(failures))
        return tuple(failures)


def start_host_services(owner, home):
    """Call only from a host lifecycle/admission boundary, never discovery or a read RPC."""
    host = vars(owner).setdefault('_plugin_service_host', ServiceHost())
    host.start_profile(home)


def close_host_service_admission(owner):
    host = getattr(owner, '_plugin_service_host', None)
    if host is not None:
        host.close_admission()


def stop_host_services(owner, *, timeout=2.0):
    host = getattr(owner, '_plugin_service_host', None)
    return host.stop(timeout=timeout) if host is not None else ()
