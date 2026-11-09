"""Explicit owning-host assembly; no native provider, recipient or model is selected here."""
from dataclasses import dataclass

from .approval import ApprovalSource
from .lifecycle import LifecycleOwner
from .port import ServingPort
from .types import Refused


class _Resources:
    def __init__(self, guard, observer):
        self.guard, self.observer = guard, observer
        self.owner, self.closed = None, False

    def close(self, deadline):
        if self.closed:
            return
        self.closed = True
        if self.owner is not None:
            self.owner.close(deadline)
        else:
            try:
                if self.observer is not None:
                    self.observer.close(deadline)
            finally:
                self.guard.close(deadline)


@dataclass(frozen=True)
class TransportBootstrap:
    """All factories are qualified host objects, not strings loaded from config or tools.

    guard_factory acquires the existing profile/namespace OS ownership and returns
    a guard with exact serving identity/death verification. observer_factory holds
    only query rights. native_factory preserves suspended spawn/Job coverage and
    kit driver validation. Missing qualification means no host installation.
    """
    guard_factory: object
    observer_factory: object
    native_factory: object
    launcher: object
    approval_transport: object
    scheduler_id: str
    controller: str
    trusted_issuers: tuple[str, ...]

    def __call__(self, scope):
        from ..service import ServingBinding
        if not self.trusted_issuers:
            raise Refused('TRUSTED_ISSUER_REQUIRED')
        guard = self.guard_factory(scope)
        resources = _Resources(guard, None)
        scope.on_close(resources.close)  # track the acquired guard before further acquisition
        resources.observer = self.observer_factory(scope)
        native = self.native_factory(scope)
        owner = LifecycleOwner(scope, guard, resources.observer, native, self.launcher,
                               scheduler_id=self.scheduler_id, controller=self.controller)
        resources.owner = owner
        source = ApprovalSource(owner, self.approval_transport, scheduler_id=self.scheduler_id)
        return ServingBinding(ServingPort(owner), source, self.trusted_issuers, self.scheduler_id)
