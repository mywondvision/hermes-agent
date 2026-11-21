"""Private Responses transport. E1 exchanges use HTTP only on numeric loopback, with inert credentials."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import http.client
import json
import socket
import subprocess
import sys
import time
from threading import Event
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from plugins.symphony_pm.transport import linux_carrier as carrier
from plugins.symphony_pm.transport import linux_carrier_probe, linux_responses_relay as relay
from plugins.symphony_pm.transport import linux_sandbox
from plugins.symphony_pm.transport.linux_bootstrap import linux_exec_transport
from plugins.symphony_pm.transport.linux_launcher import LinuxSettings
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.symphony_linux_responses_stub import responses_stub
from tests.plugins.test_symphony_pm_linux_native_contract import identity, report

pytest_plugins = ('tests.plugins.test_symphony_pm_linux_primary_unit',)


def request(server, body, *, method='POST', path=carrier.PATH, extra=None):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        headers = {'Authorization': 'Bearer fixture-token', 'chatgpt-account-id': 'fixture-account',
                   'Content-Type': 'application/json', **(extra or {})}
        connection.request(method, path, json.dumps(body).encode('ascii'), headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_real_loopback_responses_with_call_id_and_closed_grant(monkeypatch):
    nonce = 'two-turn-contract'
    monkeypatch.setattr(relay, 'PORT', 0)
    with responses_stub(nonce) as stub:
        upstream = urlsplit(stub.origin)
        assert upstream.hostname == '127.0.0.1'
        def peer(timeout):
            return socket.create_connection(('127.0.0.1', upstream.port), timeout)
        with relay.running(peer, time.monotonic() + 20) as server:
            first = {'input': [{'role': 'user', 'content': nonce}], 'stream': True,
                     'tools': [{'type': 'function', 'name': 'exec_command'}]}
            assert request(server, first)[0] == 403
            server.authorize = lambda: True
            assert request(server, first, path='http://127.0.0.1/elsewhere')[0] == 403
            assert request(server, first, method='CONNECT')[0] != 200
            assert request(server, first, method='GET', extra={'Upgrade': 'websocket'})[0] == 426
            assert request(server, first, extra={'Authorization': 'Bearer wrong'})[0] == 401
            status, payload = request(server, first)
            assert status == 200
            events = [json.loads(line[6:]) for line in payload.splitlines() if line.startswith(b'data: ')]
            call = next(e['item'] for e in events if e['type'] == 'response.output_item.done')
            assert call['type'] == 'function_call' and call['name'] == 'exec_command' and call['call_id'] == stub.call_id
            # E1 returns a synthetic tool result. Only the optional Linux CLI scenario can execute that command.
            second = {'stream': True, 'input': [{'type': 'function_call_output', 'call_id': call['call_id'],
                                                 'output': stub.expected_output}]}
            assert request(server, second)[0] == 200
            assert stub.receipt() == {'nonce': nonce, 'call_id': stub.call_id, 'responses': 2,
                                      'tool_output_source': 'synthetic'}
            server.authorize = lambda: False
            assert request(server, second)[0] == 403


def bootstrap(q, evidence=None):
    return linux_exec_transport(profile_home=q.profile_home, namespace=q.namespace, qualification=q,
        settings=LinuxSettings('project', '/repository', '/attempts', '/state', 5, 3, 1, 1, 1),
        approval_transport=object(), scheduler_id=q.scheduler_id, controller='controller', trusted_issuers=q.issuer_refs,
        provider_evidence_sha256=evidence).launcher


def test_real_exec_and_same_named_command_profile_require_explicit_carrier(installation, tmp_path):
    q, _ = installation
    q = replace(q, namespace='symphony_pm:ingress-linux')
    fingerprint = q.fingerprint
    normal = bootstrap(q)
    assert q.fingerprint == fingerprint
    provider = replace(q, environment_ref=carrier.PROFILE, command_environment=tuple(carrier.PROBE_ENV))
    with pytest.raises(Refused, match='QUALIFICATION_REQUIRED'):
        bootstrap(provider)
    with pytest.raises(Refused, match='QUALIFICATION_REQUIRED'):
        bootstrap(q, 'a'*64)
    with pytest.raises(Refused, match='SEPARATE_PRINCIPAL_REQUIRED'):
        bootstrap(provider, 'a'*64)
    baseline = normal._call(identity().ticket, SimpleNamespace(goal='g', raw_constraints=()), SimpleNamespace(src=tmp_path), {})
    argv, probe = carrier.commands(provider, {}, baseline.challenge)
    assert argv[0] == probe[0] == q.executable
    assert argv[1:1+len(carrier.options())] == probe[1:1+len(carrier.options())] == carrier.options()
    assert argv[1+len(carrier.options())] == 'exec'
    assert probe[1+len(carrier.options()):][:3] == ('sandbox', '-P', q.permission_profile)
    assert 'default_permissions=' + json.dumps(q.permission_profile) in argv
    assert 'forced_login_method="chatgpt"' in argv and 'model_provider="openai"' in argv
    assert carrier.BASE == f'https://127.0.0.1:{carrier.PORT}/backend-api'
    assert baseline.argv[:2] == (q.executable, 'exec') and not set(carrier.options()[1::2]).intersection(baseline.argv)


@pytest.mark.parametrize('change', ['valid', 'implicit-inherit', 'allow-auth', 'allow-network', 'catalog-from-work'])
def test_named_policy_requires_closed_worker_environment_and_filesystem(tmp_path, change):
    text = f'''default_permissions = "symphony-provider"
model_catalog_json = "{carrier.CATALOG}"
approval_policy = "never"
web_search = "disabled"
[shell_environment_policy]
inherit = "none"
[features]
network_proxy = false
hooks = false
plugins = false
enable_mcp_apps = false
enable_request_compression = false
[permissions.symphony-provider.network]
enabled = false
[permissions.symphony-provider.filesystem]
":root" = "read"
"/work" = "write"
"{linux_sandbox.GUEST_SCRATCH}" = "write"
"{carrier.HOME}" = "deny"
'''
    edits = {'implicit-inherit': ('inherit = "none"', ''), 'allow-auth': ('= "deny"', '= "read"'),
             'allow-network': ('enabled = false', 'enabled = true'),
             'catalog-from-work': (carrier.CATALOG, '/work/models.json')}
    config = tmp_path/'config.toml'
    if change in edits:
        config.write_text(text.replace(*edits[change]), encoding='utf-8')
        with pytest.raises(Refused, match='COMMAND_POLICY'):
            carrier.verify_config(config, 'symphony-provider')
    else:
        config.write_text(text, encoding='utf-8')
        carrier.verify_config(config, 'symphony-provider')


def test_primary_payload_rejects_forged_carrier_before_effects(tmp_path):
    from plugins.symphony_pm.transport.linux_controller import _payload
    payload_dir = tmp_path/'payload'
    payload_dir.mkdir()
    with pytest.raises(Refused, match='SEPARATE_PRINCIPAL_REQUIRED'):
        _payload(payload_dir, {'carrier': {}}, '1'*32)
    assert list(payload_dir.iterdir()) == []
    argv = linux_sandbox.sandbox_command({'bwrap': '/bwrap', 'runtime': '/image', 'workspace': '/src'}, ['client'])
    assert '--share-net' not in argv and carrier.AUTH not in argv and carrier.SOCKET not in argv


def test_optimized_python_refuses_cold_qualification_before_host_checks():
    program = """from tests.plugins.symphony_linux_provider_e2 import run
try:
    run()
except RuntimeError as exc:
    if str(exc) == 'E2 requires active assertions':
        raise SystemExit(0)
raise SystemExit(1)
"""
    subprocess.run([sys.executable, '-B', '-O', '-c', program], check=True, timeout=20)


def test_cancel_during_peer_discovery_withholds_request(monkeypatch):
    monkeypatch.setattr(relay, 'PORT', 0)
    waiting, resume = Event(), Event()
    local, upstream = socket.socketpair()
    with local, upstream:
        def peer(timeout):
            waiting.set()
            assert resume.wait(3)
            return local.dup()
        with relay.running(peer, time.monotonic() + 10) as server, ThreadPoolExecutor(max_workers=1) as pool:
            server.authorize = lambda: True
            result = pool.submit(request, server, {'stream': True})
            try:
                assert waiting.wait(3)
                server.authorize = lambda: False
            finally:
                resume.set()
            assert result.result(timeout=3)[0] == 403
            local.close()
            upstream.settimeout(2)
            assert upstream.recv(512) == b''


def test_cancel_closes_active_upstream_before_first_response_byte(monkeypatch):
    monkeypatch.setattr(relay, 'PORT', 0)
    local, upstream = socket.socketpair()
    with local, upstream, relay.running(lambda _: local.dup(), time.monotonic() + 10) as server:
        upstream.settimeout(3)
        server.authorize = lambda: True
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(request, server, {'stream': True})
            data = b''
            while b'{"stream": true}' not in data:
                chunk = upstream.recv(2048)
                assert chunk
                data += chunk
            server.authorize = lambda: False
            assert upstream.recv(1) == b''
            with pytest.raises((ConnectionResetError, ConnectionAbortedError, http.client.RemoteDisconnected)):
                future.result(timeout=3)
            # This relay has one handler thread. Serving the next request proves
            # that the handler waiting for the first upstream byte has returned.
            assert request(server, {'stream': True})[0] == 403


@pytest.mark.parametrize('fault', [None, 'same-namespace', 'scope-replay', 'endpoint_denied', 'home_denied',
                                   'connector_hidden', 'closed_fds', 'environment_exact'])
def test_worker_report_rejects_endpoint_auth_or_inherited_fds(fault):
    binding = identity()
    scope = {'unit': binding.unit, 'invocation': binding.invocation_id, 'net': 'net:[23]'}
    evidence = {'worker': report(), 'carrier': dict.fromkeys(
        ('endpoint_denied', 'home_denied', 'connector_hidden', 'closed_fds', 'environment_exact'), True)}
    if fault == 'same-namespace':
        scope['net'] = evidence['worker']['network_namespace']
    elif fault == 'scope-replay':
        scope['invocation'] = '0'*32
    elif fault:
        evidence['carrier'][fault] = False
    evidence['worker']['environment'] = list(carrier.PROBE_ENV)
    q = SimpleNamespace(command_environment=tuple(carrier.PROBE_ENV))
    if fault:
        with pytest.raises(Refused):
            linux_carrier_probe.verify(evidence, scope, binding, q, 'challenge', 'net:[12]')
    else:
        assert len(linux_carrier_probe.verify(evidence, scope, binding, q, 'challenge', 'net:[12]')) == 64
