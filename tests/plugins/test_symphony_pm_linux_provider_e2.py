"""Explicit native first qualification in the purpose-built, fake-only systemd guest."""
import http.client
import json
import os
from pathlib import Path
import ssl
import time
from urllib.parse import urlsplit

import pytest


def test_cold_fixture_passes_both_responses_through_packaged_connector(tmp_path):
    from tests.plugins.symphony_linux_provider_e2 import local_endpoints, synthetic_files, verify_policy_order

    nonce = 'cold-wire'
    tls, cert, _ = synthetic_files(tmp_path)
    client_tls = ssl.create_default_context(cafile=str(cert))
    scope = {'net': 'synthetic-scope'}
    with local_endpoints(nonce, tls, cert, 'NO_CONSTRAINT', None, lambda: scope, 0) as (ingress, upstream, calls):
        port = urlsplit(ingress.origin).port
        def request(method, path, body=None):
            connection = http.client.HTTPSConnection('127.0.0.1', port, timeout=3, context=client_tls)
            try:
                wire = json.dumps(body).encode('ascii') if body is not None else None
                connection.request(method, path, wire, {'authorization': 'Bearer fixture-token',
                                   'chatgpt-account-id': 'fixture-account', 'content-type': 'application/json'})
                response = connection.getresponse()
                assert response.status == 200
                return response.read()
            finally:
                connection.close()

        policy = json.loads(request('GET', '/backend-api/wham/accounts/check'))
        assert policy['accounts'][0]['workspace_backend_origin'] == 'NO_CONSTRAINT' and not calls
        request('POST', ingress.route, {'input': [{'role': 'user', 'content': nonce}], 'stream': True,
                'tools': [{'type': 'function', 'name': 'exec_command'}]})
        request('POST', ingress.route, {'input': [{'type': 'function_call_output', 'call_id': upstream.call_id,
                                                  'output': upstream.expected_output}], 'stream': True})
        assert ingress.responses == 0 and ingress.policy_requests == 1
        verify_policy_order(ingress, upstream, expected_posts=2)
        assert upstream.receipt()['tool_output_source'] == 'synthetic'
        assert len(calls) == upstream.responses == 2
        assert all(call['net'] == 'synthetic-scope' and call['peer'][0] == '127.0.0.1' for call in calls)


@pytest.mark.platforms('linux')
def test_cold_chatgpt_routing_and_worker_isolation(monkeypatch):
    from plugins.symphony_pm.transport.linux_control import strict_json
    from plugins.symphony_pm.transport.types import digest
    from tests.plugins.symphony_linux_provider_e2 import UNIT_PATH, run, systemd_invocation

    if os.fsdecode(Path('/proc/self/cgroup').read_bytes()).strip() != '0::' + UNIT_PATH:
        pytest.skip('requires explicit invocation in the dedicated provider E2 unit')
    # The standard runner starts pytest with env -i. Restore only the manager's verified
    # identifier for our observed unit before invoking its normal cgroup supervisor.
    invocation = systemd_invocation()
    monkeypatch.setenv('INVOCATION_ID', invocation)
    started = time.monotonic()
    directory = run()
    receipt = strict_json(directory/'cold-e2.json')
    binding_record = strict_json(directory/'binding.json')
    binding, request = binding_record['binding'], binding_record['request']
    stop = strict_json(directory/'stop')
    assert binding['service_id'] == invocation and binding['key'] == request['nonce'] == directory.name
    assert started <= request['started'] <= stop['observed'] <= time.monotonic()
    assert binding['request_sha256'] == digest(request)
    assert binding['host'] == request['inputs'] == digest(receipt['inputs'])
    assert receipt['binding'] == stop['binding'] == digest(binding)
    assert receipt['stop'] == digest(stop)
    assert receipt['observations'] == strict_json(directory/'observations.json')
