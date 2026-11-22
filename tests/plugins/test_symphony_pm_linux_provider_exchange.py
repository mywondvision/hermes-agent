"""Two Responses requests through the production relay and connector to an offline TLS upstream."""
import asyncio
import http.client
import json
import socket
import ssl
import time
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from plugins.symphony_pm.transport import linux_responses_connector as connector
from plugins.symphony_pm.transport import linux_responses_relay as relay
from tests.plugins.symphony_linux_responses_stub import responses_stub, valid_worker_environment
from tests.plugins.test_symphony_pm_linux_provider_carrier import request
from tests.plugins.test_symphony_pm_linux_responses_connector import _certificate, _listener


def fixture_request(origin, context, method, path, token, body=None):
    with socket.create_connection(('127.0.0.1', urlsplit(origin).port), 3) as local:
        with context.wrap_socket(local, server_hostname='chatgpt.com') as secured:
            conn = http.client.HTTPConnection('fixture', timeout=3)
            conn.auto_open = False
            conn.sock = secured
            try:
                conn.request(method, path, body, headers={'Authorization': 'Bearer ' + token,
                                                         'chatgpt-account-id': 'fixture-account'})
                response = conn.getresponse()
                return response.status, response.getheader('Content-Type'), response.read()
            finally:
                conn.close()


def test_two_turn_exchange_through_relay_and_tls_connector(tmp_path, monkeypatch):
    nonce = 'offline-relay-connector'
    cert, key = _certificate(tmp_path, 'chatgpt.com', 'loopback')
    monkeypatch.setattr(connector, 'CA_FILE', str(cert))
    monkeypatch.setattr(relay, 'PORT', 0)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert, key)
    client_tls = connector._tls_context()
    destinations = []

    with responses_stub(nonce, tls=server_tls) as stub:
        local = urlsplit(stub.origin)
        assert local.hostname == '127.0.0.1' and local.scheme == 'https'

        async def dial(host, port, *, ssl, server_hostname, **kwargs):
            assert (host, port, server_hostname) == ('chatgpt.com', 443, 'chatgpt.com')
            assert ssl is client_tls and ssl.check_hostname
            destinations.append((host, port))
            return await asyncio.open_connection('127.0.0.1', local.port, ssl=ssl,
                                                 server_hostname=server_hostname, **kwargs)

        monkeypatch.setattr(connector, 'open_connection', dial)

        def exchange(port):
            def local_peer(timeout):
                # Linux verifies peer credentials on its Unix socket before returning this stream.
                return socket.create_connection(('127.0.0.1', port), timeout)

            with relay.running(local_peer, time.monotonic() + 15) as ingress:
                ingress.authorize = lambda: True
                first = {'stream': True, 'input': [{'role': 'user', 'content': nonce}],
                         'tools': [{'type': 'function', 'name': 'exec_command'}]}
                code, body = request(ingress, first)
                assert code == 200
                events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith(b'data: ')]
                call = next(event['item'] for event in events if event['type'] == 'response.output_item.done')
                assert (call['type'], call['name'], call['call_id']) == ('function_call', 'exec_command', stub.call_id)
                # Native qualification supplies this result by executing the delivered command on Linux.
                result = {'type': 'function_call_output', 'call_id': call['call_id'], 'output': stub.expected_output}
                assert request(ingress, {'stream': True, 'input': [result]})[0] == 200

        async def run():
            async def peer(reader, writer):
                await connector._serve_request(reader, writer, client_tls)
            async with _listener(peer, limit=connector.HEAD_LIMIT) as port:
                await asyncio.wait_for(asyncio.to_thread(exchange, port), 20)

        asyncio.run(run())
        assert stub.receipt() == {'nonce': nonce, 'call_id': stub.call_id, 'responses': 2,
                                  'tool_output_source': 'synthetic'}
        assert destinations == [('chatgpt.com', 443)] * 2


def test_fake_tls_policy_is_authenticated_and_preserves_conflict(tmp_path):
    cert, key = _certificate(tmp_path, 'chatgpt.com', 'policy')
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert, key)
    client_tls = ssl.create_default_context(cafile=str(cert))
    for origin in ('NO_CONSTRAINT', 'https://conflict.invalid'):
        with responses_stub('policy', tls=server_tls, policy_origin=origin) as stub:
            for auth, expected in (('wrong', 401), ('fixture-token', 200)):
                status, _, body = fixture_request(stub.origin, client_tls, 'GET', '/backend-api/wham/accounts/check', auth)
                assert status == expected
                if expected == 200:
                    assert json.loads(body)['accounts'][0]['workspace_backend_origin'] == origin
        assert stub.policy_requests == 1
        assert stub.request_methods == ['GET', 'GET'] and stub.first_policy_index == 1


def test_fixture_https_ingress_forwards_only_authenticated_wire_bytes(tmp_path):
    cert, key = _certificate(tmp_path, 'chatgpt.com', 'ingress')
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cafile=str(cert))
    seen = []

    def forward(body, headers):
        seen.append((body, headers['chatgpt-account-id']))
        return 202, 'text/event-stream', b'fixture stream'

    with pytest.raises(ValueError, match='fixture account'), responses_stub('e2', token='other', tls=server,
                                                                           forward_post=forward):
        pytest.fail('unexpected fixture account')
    with responses_stub('e2', tls=server, forward_post=forward, port=0) as ingress:
        for token, status in (('wrong', 401), ('fixture-token', 202)):
            code, content_type, data = fixture_request(ingress.origin, client, 'POST', ingress.route, token,
                                                      b'{ "fixture" : true }')
            assert code == status
            if status == 202:
                assert (content_type, data) == ('text/event-stream', b'fixture stream')
        assert ingress.request_methods == ['POST', 'POST']
    assert seen == [(b'{ "fixture" : true }', 'fixture-account')]


def test_native_tool_readback_requires_completed_process_and_independent_namespaces():
    from tests.plugins.symphony_linux_responses_stub import ResponsesStub

    def namespaces(inode):
        return {kind: f'{kind}:[{inode}]' for kind in ('net', 'mnt', 'pid')}

    stub = ResponsesStub('native-evidence', lambda: namespaces(3))
    marker = stub.expected_output + ' ' + json.dumps(namespaces(7))
    assert not stub._returned_marker(marker)
    assert not stub._returned_marker('Process exited with code 1\n' + marker)
    assert not stub._returned_marker('Process exited with code 0\nProcess running with session ID 1\n' + marker)
    assert not stub._returned_marker('Process exited with code 0\n' + stub.expected_output + ' ' + json.dumps(namespaces(3)))
    assert stub._returned_marker('Wall time: 0.1 seconds\nProcess exited with code 0\nOutput:\n' + marker)


def test_worker_accepts_pinned_tool_defaults_and_refuses_overrides():
    env = dict(CODEX_SANDBOX_NETWORK_DISABLED='1', CODEX_VERSION='0.158.0',
               CODEX_PERMISSION_PROFILE='symphony-provider', NO_COLOR='1', TERM='dumb', LANG='C.UTF-8',
               LC_CTYPE='C.UTF-8', LC_ALL='C.UTF-8', COLORTERM='', PAGER='cat', GIT_PAGER='cat',
               GH_PAGER='cat', CODEX_CI='1', CODEX_THREAD_ID=str(uuid4()), CODEX_SESSION_ID=str(uuid4()))
    assert valid_worker_environment(env)
    for change in ({'PAGER': 'less'}, {'CODEX_VERSION': 'other'}, {'CODEX_SESSION_ID': 'invalid'},
                   {'UNEXPECTED': 'value'}):
        assert not valid_worker_environment(env | change)
