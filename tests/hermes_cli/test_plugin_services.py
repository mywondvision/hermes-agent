"""Service lifetime contracts; pure injected factories, no worker/model/network calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace

import pytest

from hermes_cli import plugins
from hermes_cli.plugin_request_binding import (
    AuthorityGrant, RequestIdentity, bind_request, canonical_home, current_authority,
)
from hermes_cli.plugin_services import ServiceHost
from hermes_constants import get_hermes_home


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    manager = plugins.PluginManager(scope_key=str(tmp_path))
    ctx = plugins.PluginContext(plugins.PluginManifest(name='runtime-test', source='user',
                                                     path=str(tmp_path)), manager)
    monkeypatch.setattr(plugins, 'get_plugin_manager', lambda: manager)
    monkeypatch.setattr(plugins, 'discover_plugins', lambda: None)
    host = ServiceHost()
    yield manager, ctx, host, tmp_path
    host.stop()
    manager.unload()


def _scope(runtime):
    _, ctx, host, home = runtime
    ref = ctx.register_runtime_service('local', lambda scope: scope)
    host.start_profile(home)
    return ref.get()


def _grant(scope):
    identity = RequestIdentity(scope.profile_home, scope.namespace, 'session-1', 'request-1', 'a'*64)
    return identity, AuthorityGrant(identity, 'issuer', 'grant', 'b'*64, 110.0, object())


def test_discovery_is_inert_and_start_is_idempotent(runtime):
    _, ctx, host, home = runtime
    opened, closed = [], []

    def factory(scope):
        opened.append((get_hermes_home().resolve(), scope.generation))
        scope.on_close(lambda _deadline: closed.append(get_hermes_home().resolve()))
        return object()

    ref = ctx.register_runtime_service('local', factory)
    assert ref.get() is None and opened == []
    host.start_profile(home)
    value = ref.get()
    assert value is not None
    host.start_profile(home)
    assert ref.get() is value and len(opened) == 1
    host.close_admission()
    assert ref.get() is None and closed == []
    host.stop()
    host.stop()
    assert closed == [home.resolve()]


def test_startup_failure_cleans_resources_without_retry(runtime):
    _, ctx, host, home = runtime
    events = []

    def factory(scope):
        events.append('open')
        scope.on_close(lambda _deadline: events.append('close'))
        raise RuntimeError('factory failed')

    ref = ctx.register_runtime_service('local', factory)
    host.start_profile(home)
    host.start_profile(home)
    assert ref.get() is None and events == ['open', 'close']


def test_cleanup_failure_does_not_skip_other_resources(runtime):
    manager, ctx, host, home = runtime
    events = []

    def factory(scope):
        scope.on_close(lambda _deadline: events.append('last'))

        def fail(_deadline):
            assert not scope.active
            raise RuntimeError('close failed')

        scope.on_close(fail)
        return scope

    ctx.register_runtime_service('local', factory)
    host.start_profile(home)
    assert host.stop() == ('RuntimeError',)
    assert events == ['last']
    assert host.stop() == ('RuntimeError',)  # the unresolved outcome is not reset or retried
    manager.unload('runtime-test')
    with pytest.raises(ValueError, match='duplicate'):
        ctx.register_runtime_service('local', lambda scope: scope)


def test_unload_revokes_before_tool_disposal_and_does_not_retarget(runtime):
    manager, ctx, host, home = runtime
    ref = ctx.register_runtime_service('local', lambda scope: scope)
    host.start_profile(home)
    scope = ref.get()
    events = []
    ctx._track('tool', 'late-tool', lambda: events.append(scope.active))
    manager.unload('runtime-test')
    assert events == [False] and ref.get() is None
    next_ref = ctx.register_runtime_service('local', lambda next_scope: next_scope)
    host.start_profile(home)
    assert next_ref.get().generation != scope.generation and ref.get() is None


def test_abandoned_loader_cannot_declare_a_service(runtime):
    _, ctx, host, home = runtime
    ctx._abandon_load()
    assert ctx.register_runtime_service('local', lambda scope: scope) is None
    host.start_profile(home)


def test_second_host_cannot_take_over_a_live_profile(runtime):
    _, ctx, host, home = runtime
    ctx.register_runtime_service('local', lambda scope: scope)
    host.start_profile(home)
    with pytest.raises(RuntimeError, match='another host'):
        ServiceHost().start_profile(home)


def test_request_lease_is_revoked_on_return_expiry_and_host_stop(runtime):
    scope = _scope(runtime)
    identity, grant = _grant(scope)
    clock = [100.0]
    assert current_authority(scope) is None
    with bind_request(scope, identity, grant, clock=lambda: clock[0]):
        assert current_authority(scope) is grant.value
        inherited = copy_context()
        clock[0] = 110.0
        assert current_authority(scope) is None
        clock[0] = 100.0
        runtime[2].close_admission()
        assert current_authority(scope) is None
    assert inherited.run(current_authority, scope) is None


@pytest.mark.parametrize('field,value', [('profile_home', 'foreign'), ('namespace', 'foreign'),
                                         ('session_id', ''), ('request_id', ''), ('payload_digest', '')])
def test_mismatched_or_incomplete_request_is_refused(runtime, field, value):
    scope = _scope(runtime)
    identity, grant = _grant(scope)
    changed = replace(identity, **{field: value})
    with pytest.raises(PermissionError):
        with bind_request(scope, changed, replace(grant, identity=changed), clock=lambda: 100.0):
            pytest.fail('invalid binding admitted')


def test_copied_context_cannot_authorize_another_thread(runtime):
    scope = _scope(runtime)
    identity, grant = _grant(scope)
    with bind_request(scope, identity, grant, clock=lambda: 100.0):
        inherited = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(inherited.run, current_authority, scope).result(timeout=5) is None
        assert current_authority(scope) is grant.value


def test_detached_task_cannot_inherit_authority(runtime):
    scope = _scope(runtime)
    identity, grant = _grant(scope)

    async def exercise():
        async def read():
            return current_authority(scope)
        with bind_request(scope, identity, grant, clock=lambda: 100.0):
            assert current_authority(scope) is grant.value
            assert await asyncio.create_task(read()) is None
        assert current_authority(scope) is None
    asyncio.run(exercise())


def test_two_profiles_use_distinct_factories_and_lifetimes(tmp_path, monkeypatch):
    homes = [tmp_path/'first', tmp_path/'second']
    managers = {canonical_home(h): plugins.PluginManager(scope_key=str(h)) for h in homes}
    monkeypatch.setattr(plugins, 'discover_plugins', lambda: None)
    monkeypatch.setattr(plugins, 'get_plugin_manager', lambda: managers[canonical_home(get_hermes_home())])
    refs = []
    for home in homes:
        manager = managers[canonical_home(home)]
        ctx = plugins.PluginContext(plugins.PluginManifest(name='runtime-test', source='user', path=str(home)), manager)
        refs.append(ctx.register_runtime_service('local', lambda scope: scope))
    host = ServiceHost()
    try:
        for home in homes:
            host.start_profile(home)
        first, second = (ref.get() for ref in refs)
        assert first.profile_home != second.profile_home and first.generation != second.generation
        identity, grant = _grant(first)
        with bind_request(first, identity, grant, clock=lambda: 100.0):
            assert current_authority(second) is None
    finally:
        host.stop()
        for manager in managers.values():
            manager.unload()


def test_pinned_remote_host_refuses_live_service_registration():
    from hermes_cli.plugin_host_child import RemotePluginContext
    from hermes_cli.plugin_host_wire import PluginHostUnsupported
    remote = RemotePluginContext(None, 'runtime-test', {'ctx_methods': ['register_runtime_service']})
    with pytest.raises(PluginHostUnsupported):
        remote.register_runtime_service('local', lambda scope: scope)
