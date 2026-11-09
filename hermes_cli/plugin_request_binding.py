"""Host-issued request authority with revocable, thread/task-local lifetime.

This is an in-process binding, not an issuer or a permission store. No tool argument,
session identifier or configuration value creates an AuthorityGrant here.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time
from typing import Any, Callable

from hermes_constants import hermes_home_key


def canonical_home(home: str | Path) -> str:
    path = Path(home)
    if not path.is_absolute():
        raise ValueError('service home must be an explicit absolute path')
    return hermes_home_key(path.resolve())


@dataclass(frozen=True)
class RequestIdentity:
    profile_home: str
    namespace: str
    session_id: str
    request_id: str
    payload_digest: str


@dataclass(frozen=True)
class AuthorityGrant:
    identity: RequestIdentity
    issuer_ref: str
    grant_ref: str
    contract_digest: str
    expires_at: float
    value: Any


def _task():
    try:
        return asyncio.current_task()
    except RuntimeError:  # a synchronous tool worker has no event loop
        return None


@dataclass
class _Lease:
    scope: Any
    grant: AuthorityGrant
    thread: int
    task: Any
    clock: Callable[[], float]
    active: bool = True


_CURRENT: ContextVar[_Lease | None] = ContextVar('plugin_runtime_request', default=None)


def _valid(scope, identity: RequestIdentity, grant: AuthorityGrant, now: float) -> bool:
    return (type(identity) is RequestIdentity and type(grant) is AuthorityGrant
            and grant.identity == identity and identity.profile_home == scope.profile_home
            and identity.namespace == scope.namespace
            and all(type(v) is str and bool(v) for v in (
                identity.session_id, identity.request_id, identity.payload_digest,
                grant.issuer_ref, grant.grant_ref, grant.contract_digest))
            and type(grant.expires_at) in (int, float) and math.isfinite(grant.expires_at)
            and math.isfinite(now) and now < grant.expires_at)


@contextmanager
def bind_request(scope, identity: RequestIdentity, grant: AuthorityGrant, *, clock=time.time):
    """Only the trusted host/consumer calls this after checking its issuer's receipt.

    The caller must not populate a grant from tool arguments. A nested scope cannot
    retarget an outer one. Copied contexts in threads/detached tasks cannot read it.
    """
    if not scope.active or not _valid(scope, identity, grant, clock()):
        raise PermissionError('runtime request binding refused')
    lease = _Lease(scope, grant, threading.get_ident(), _task(), clock)
    token = _CURRENT.set(lease)
    try:
        yield
    finally:
        lease.active = False  # also revokes contexts copied before the reset
        _CURRENT.reset(token)


def current_authority(scope):
    lease = _CURRENT.get()
    if (lease is None or not lease.active or lease.scope is not scope or not scope.active
            or lease.thread != threading.get_ident() or lease.task is not _task()):
        return None
    if not _valid(scope, lease.grant.identity, lease.grant, lease.clock()):
        return None
    return lease.grant.value
