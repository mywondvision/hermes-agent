"""Consumer of generic host services; no runtime, grant or driver is constructed here.

An owning host may inject ServingBinding before bootstrap. Without that binding the
existing PM tool stays unwired. A production approval source is still a separate
qualification; this module never derives authority from a model's arguments.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from hermes_cli.plugin_request_binding import (
    AuthorityGrant, RequestIdentity, bind_request, current_authority,
)
from tools.symphony_pm_adapter import (
    SymphonyPmAdapter, SymphonyPmPort, TrustedPmContextV1, make_pm_tool_handler,
    parse_intent, register_pm_tool,
)


@dataclass(frozen=True)
class ServingBinding:
    port: SymphonyPmPort
    authority_source: Callable[[RequestIdentity], AuthorityGrant | None]
    trusted_issuers: tuple[str, ...]
    scheduler_id: str


@dataclass(frozen=True)
class ServingBootstrap:
    factory: Callable


class _LivePort:
    def __init__(self, scope, port):
        self.scope, self.port = scope, port

    def _call(self, name, request, context):
        if not self.scope.active or current_authority(self.scope) is None:
            raise RuntimeError('PM service unavailable outside its authorized request')
        return getattr(self.port, name)(request, context)

    def submit_intent(self, request, context):
        return self._call('submit_intent', request, context)

    def get_status(self, request, context):
        return self._call('get_status', request, context)

    def request_cancel(self, request, context):
        return self._call('request_cancel', request, context)


class ServingIngress:
    def __init__(self, ctx, scope):
        self.scope = scope
        self.binding = scope.binding
        if type(self.binding) is ServingBootstrap:
            self.binding = self.binding.factory(scope)
        if self.binding is None:
            return  # the host has not installed an issuer/port; keep the original inert tool
        if (type(self.binding) is not ServingBinding or self.binding.port is None
                or not callable(self.binding.authority_source)
                or not self.binding.trusted_issuers or not self.binding.scheduler_id):
            raise ValueError('invalid PM serving binding')
        self.adapter = SymphonyPmAdapter(
            _LivePort(scope, self.binding.port), trusted_issuers=self.binding.trusted_issuers,
            scheduler_id=self.binding.scheduler_id)
        self.handler = make_pm_tool_handler(self.adapter, lambda: current_authority(scope))
        self.without_authority = make_pm_tool_handler(self.adapter, lambda: None)
        register_pm_tool(_ServiceRegistry(ctx, self), adapter=self.adapter,
                         context_source=lambda: current_authority(scope))

    def handle(self, args, **runtime):
        if not self.scope.active:
            return make_pm_tool_handler()(args)
        intent, _, _ = parse_intent(args)
        if intent is None:
            return self.without_authority(args, **runtime)
        # Registry kwargs come from model_tools' host-owned _CallIds, never tool
        # args or a process-global session/env fallback (unsafe across profiles).
        session_id = runtime.get('session_id')
        if type(session_id) is not str or not session_id:
            return self.without_authority(args, **runtime)
        identity = RequestIdentity(self.scope.profile_home, self.scope.namespace,
                                   session_id, intent.request_id, intent.payload_digest)
        try:
            grant = self.binding.authority_source(identity)
        except Exception:  # health: allow BLE001 -- issuer failures deny without exposing exception text
            return self.without_authority(args, **runtime)
        if not self._bound_context(grant, identity):
            return self.without_authority(args, **runtime)  # no ambient outer grant fallback
        try:
            with bind_request(self.scope, identity, grant):
                return self.handler(args, **runtime)
        except PermissionError:
            return make_pm_tool_handler()(args)

    def _bound_context(self, grant, identity):
        if type(grant) is not AuthorityGrant or grant.identity != identity:
            return False
        context = grant.value
        return (type(context) is TrustedPmContextV1
                and context.issuer_ref == grant.issuer_ref
                and context.issuer_ref in self.binding.trusted_issuers
                and context.grant_ref == grant.grant_ref
                and context.contract_digest == grant.contract_digest
                and context.expires_at == grant.expires_at
                and context.request_id == identity.request_id
                and context.approved_payload_digest == identity.payload_digest)


class _ServiceRegistry:
    def __init__(self, ctx, consumer):
        self.ctx, self.consumer = ctx, consumer

    def register(self, **kwargs):
        kwargs.update(handler=self.consumer.handle, check_fn=lambda: self.consumer.scope.active)
        handle = self.ctx.register_tool(**kwargs)
        if handle is None:
            raise RuntimeError('Hermes refused the serving PM tool')
        # Stop leaves this generation's handler in place but unavailable. Cleanup
        # restores registrations only after admission closes, through the normal ledger.
        self.consumer.scope.on_close(lambda _deadline: handle.dispose())
