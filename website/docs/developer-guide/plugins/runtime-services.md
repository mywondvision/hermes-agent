---
sidebar_label: Runtime services
title: Host-owned plugin runtime services
---

# Host-owned runtime services

`ctx.register_runtime_service(name, factory)` declares a profile-owned service without starting
it. Discovery and diagnostic commands allocate only a declaration. The factory receives a
`ServiceScope` when an owning serving host explicitly starts that profile, before accepting
conversations. Existing plugins that do not declare services keep their existing behavior.

```python
def register(ctx):
    ctx.register_runtime_service("integration", open_integration)

def open_integration(scope):
    resource = acquire_for_profile(scope.profile_home)
    scope.on_close(lambda deadline: resource.close(deadline=deadline))
    return resource
```

This example's acquisition is application code, not a Hermes-provided grant. Register cleanup
before publishing a resource. Cleanup callbacks run in the owning home's scope, in reverse order,
and receive an absolute monotonic deadline. They must honor it. A callback which hangs still
depends on the outer host watchdog; the service layer does not create another watchdog process.
Cleanup errors are reported and do not skip the remaining callbacks. Closing handles is not a
worker cancellation acknowledgement.

Failed cleanup keeps the exact callback references and blocks a replacement declaration for
that service key. Repeated shutdown does not retry an uncertain effect or clear its failure.

`ServiceScope` supplies immutable `profile_home`, `namespace`, `generation` and a host-injected
`binding`. `scope.active` becomes false before shutdown/unload starts disposing tool registrations.
An old `ServiceReference` never points to a replacement generation. A failed startup is closed
without an implicit retry. Force rediscovery only declares new generations; the host must explicitly
reconcile them at a safe boundary before new conversations. No tool surface is refreshed per turn.

`ServiceHost.configure(home, bindings)` supplies explicit in-process objects, keyed by
`(plugin_id, service_name)`, before `start_profile(home)`. Bindings are frozen after that start.
Configuration, tool arguments and discovery cannot install an authority source. The existing
`symphony_pm` plugin is the first consumer; without a host-installed port and issuer it stays
UNAVAILABLE. The service layer does not construct a scheduler, driver, native worker or permissions.

Owning hosts start services at the messaging gateway bootstrap (including served secondary
profiles), TUI stdio bootstrap, and standalone HTTP lifespan. An HTTP surface attached to an existing
gateway uses that gateway's owner, so it does not start a second service host. The legacy standalone
TUI/HTTP host boots its launch profile; extra legacy profile mounts and rediscovery require an
explicit safe host reconciliation. A diagnostic profile lookup does not start services for it.

The API is refused under `plugins.isolation: host` because live ports and cleanup callbacks cannot
cross that boundary. It never changes the operator's pinned isolation setting.

## Request authority

`hermes_cli.plugin_request_binding` provides `RequestIdentity`, `AuthorityGrant`, `bind_request`
and `current_authority`. Only a host-verified issuer may supply a grant. These classes represent a
binding, not an authentication mechanism, approval UI, durable permit store or execution permission.
The production issuer and its native model/environment/attempt permit must be qualified separately.

A binding includes the exact profile, service namespace, session, request, approved payload digest,
issuer, grant reference, contract digest and expiry. Invalid or expired bindings are refused.
Authority is revoked on scope exit, service shutdown and unload. Copied contexts in another thread
or a detached asyncio task cannot inherit authority, including while the parent is still active.
Explicit thread hops need a fresh host-owned binding, not automatic forwarding. A session ID, cwd,
model text or boolean confirmation cannot mint one.

The PM consumer additionally checks the grant's immutable `TrustedPmContextV1` against the request
and its configured issuer before calling the existing adapter. A source failure or denial cannot
fall back to an outer request's context. The adapter keeps its existing digest and receipt checks.
This mechanism proves neither durable dispatch nor native worker cancellation; those remain the
responsibility of the separately qualified lifecycle owner and coordinator.
