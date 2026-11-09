"""Real discovery -> explicit host bootstrap -> PM adapter, with a fixture-only issuer/port.

No native worker, model, audio or network. These contracts do not qualify a production
approval source, the durable journal, cancellation or a Symphony transport deployment.
"""
import importlib
import json
import os
from pathlib import Path
import time

import pytest

from hermes_cli import plugins
from hermes_cli.plugin_request_binding import AuthorityGrant
from hermes_cli.plugin_services import ServiceHost
from tests.tools.test_symphony_pm_adapter import FakeScheduler, ISSUER, SCHED, ctx, submit_payload
from tools import symphony_pm_adapter as pm
from tools.registry import registry


@pytest.fixture
def loaded():
    home = Path(os.environ['HERMES_HOME'])
    (home/'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['symphony_pm']}}), encoding='utf-8')
    plugins._reset_plugin_managers_for_tests()
    plugins.discover_plugins()
    manager = plugins.get_plugin_manager()
    plugin = manager._plugins['symphony_pm']
    assert plugin.enabled and not plugin.error
    service = importlib.import_module(f'{plugin.module.__name__}.service')
    host = ServiceHost()
    yield home, host, service
    host.stop()
    plugins._reset_plugin_managers_for_tests()


class _ReceiptPort(FakeScheduler):
    def common(self, intent, context, **extra):
        return {**super().common(intent, context, **extra), 'observed_at': context.now}


def _call(payload, *, session_id='fixture-session'):
    return json.loads(registry.get_entry(pm.TOOL_NAME).handler(payload, session_id=session_id))


def _bind(loaded, *, issuer=ISSUER):
    home, host, service = loaded
    payload = submit_payload()
    port = _ReceiptPort()
    approved, _, _ = pm.parse_intent(payload)

    def source(identity):
        # Only this predetermined fixture digest/session is granted; ASR/model text
        # never creates a grant. A production source has not been installed.
        if identity.payload_digest != approved.payload_digest or identity.session_id != 'fixture-session':
            return None
        now = time.time()
        context = ctx(payload, now=now, expires_at=now+60)
        return AuthorityGrant(identity, issuer, context.grant_ref, context.contract_digest,
                              context.expires_at, context)

    binding = service.ServingBinding(port, source, (ISSUER,), SCHED)
    host.configure(home, {('symphony_pm', 'ingress'): binding})
    host.start_profile(home)
    return port, payload


def test_discovery_and_unconfigured_host_remain_inert(loaded):
    home, host, _ = loaded
    assert not registry.get_entry(pm.TOOL_NAME).check_fn()
    assert _call(submit_payload())['outcome'] == 'UNAVAILABLE'
    host.start_profile(home)
    assert not registry.get_entry(pm.TOOL_NAME).check_fn()
    assert _call(submit_payload())['outcome'] == 'UNAVAILABLE'


def test_explicit_binding_dispatches_once_and_revokes_captured_handler_on_stop(loaded):
    port, payload = _bind(loaded)
    entry = registry.get_entry(pm.TOOL_NAME)
    assert entry.check_fn()
    result = _call(payload)
    assert result['outcome'] == 'SUBMITTED' and not result['hold']
    assert len(port.calls) == 1
    captured = entry.handler
    loaded[1].stop()
    assert json.loads(captured(payload))['outcome'] == 'UNAVAILABLE'
    assert len(port.calls) == 1


def test_model_payload_change_does_not_mint_a_grant(loaded):
    port, payload = _bind(loaded)
    result = _call({**payload, 'goal': 'a different unapproved request'})
    assert result['outcome'] == 'WAITING_FOR_AUTHORITY' and result['hold']
    assert port.calls == []


def test_wrong_issuer_and_missing_session_refuse_before_port(loaded):
    port, payload = _bind(loaded, issuer='wrong-issuer')
    assert _call(payload)['hold'] and port.calls == []
    assert _call(payload, session_id='')['hold'] and port.calls == []


def test_host_defers_bootstrap_until_start_and_closes_owned_resource(loaded):
    home, host, service = loaded
    port = _ReceiptPort()
    lifecycle = []

    def factory(scope):
        lifecycle.append(('start', scope.profile_home))
        scope.on_close(lambda _deadline: lifecycle.append(('close', scope.profile_home)))
        return service.ServingBinding(port, lambda _identity: None, (ISSUER,), SCHED)

    host.configure(home, {('symphony_pm', 'ingress'): service.ServingBootstrap(factory)})
    assert lifecycle == [] and _call(submit_payload())['outcome'] == 'UNAVAILABLE'
    host.start_profile(home)
    host.start_profile(home)
    assert len(lifecycle) == 1
    assert _call(submit_payload())['outcome'] == 'WAITING_FOR_AUTHORITY' and port.calls == []
    host.stop()
    assert [event for event, _ in lifecycle] == ['start', 'close']
    assert _call(submit_payload())['outcome'] == 'UNAVAILABLE'


def test_failed_bootstrap_closes_acquired_resource_and_does_not_retry(loaded):
    home, host, service = loaded
    lifecycle = []

    def factory(scope):
        lifecycle.append('acquired')
        scope.on_close(lambda _deadline: lifecycle.append('closed'))
        raise RuntimeError('fixture bootstrap failure')

    host.configure(home, {('symphony_pm', 'ingress'): service.ServingBootstrap(factory)})
    host.start_profile(home)
    host.start_profile(home)
    assert lifecycle == ['acquired', 'closed']
    assert _call(submit_payload())['outcome'] == 'UNAVAILABLE'
