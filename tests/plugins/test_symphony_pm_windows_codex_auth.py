"""File-based Codex authentication under the MXC command sandbox, on decoys only. Requires an explicit native grant.

A temporary CODEX_HOME stores a decoy API key in `auth.json` (file store, never the OS credential store); the
Codex client gets an environment with no key, plus one decoy token variable to see whether
`shell_environment_policy` filters what `codex sandbox` hands to commands. The sandboxed probe then reports
its environment (names and value digests only) and tries to read the auth file and the client's home.

No model is called and no real key exists here. Whether a real key or a ChatGPT session in that file
actually authenticates a request, and whether `codex exec` applies the same environment policy, cannot be
shown without a model call and stays UNKNOWN. Other processes of the same user are outside this boundary.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import uuid

import pytest

from tests.plugins.symphony_native_crash_support import BoundedApi, save_proof
from tests.plugins.test_symphony_pm_windows_codex_sandbox import (
    CODEX, Client, _client_environment, _host_state, _probe_python, _toml_path,
)
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')

AUTH_PROBE = r'''
import hashlib, json, os, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
out = {'steps': []}
def step(name, action):
    record = {'seq': len(out['steps'])+1, 'name': name}
    try:
        record.update(action() or {})
        record.setdefault('ok', True)
    except OSError as exc:
        record.update(ok=False, winerror=getattr(exc, 'winerror', None), errno=exc.errno)
    except Exception as exc:
        record.update(ok=False, exception=type(exc).__name__)
    out['steps'].append(record)
def opened(path):
    with open(path, 'rb'):
        pass
step('a1-env', lambda: {'names': sorted(os.environ),
                        'value_digests': sorted(hashlib.sha256(v.encode('utf-8')).hexdigest() for v in os.environ.values())})
step('a2-read-auth', lambda: opened(plan['auth']))
step('a3-list-home', lambda: {'count': len(os.listdir(plan['home']))})
step('a4-read-config', lambda: opened(plan['config']))
report = Path(plan['attempt'])/'report.json'
report.with_suffix('.tmp').write_text(json.dumps(out), encoding='utf-8')
report.with_suffix('.tmp').replace(report)
'''
EXPECTED = ['a1-env', 'a2-read-auth', 'a3-list-home', 'a4-read-config']
POLICY_DECOY = 'SYMPHONY_DECOY_TOKEN'


def _auth_home(directory, attempt, readable):
    home = directory/'codex-home'
    home.mkdir()
    lines = ['default_permissions = "symphony-probe"', 'cli_auth_credentials_store = "file"', '',
             '[shell_environment_policy]', 'inherit = "core"', 'ignore_default_excludes = false', '',
             '[windows]', 'sandbox = "mxc"', '',
             '[permissions.symphony-probe.filesystem]', '":root" = "deny"', '":minimal" = "read"',
             f'{_toml_path(attempt)} = "write"', *(f'{_toml_path(p)} = "read"' for p in readable), '',
             '[permissions.symphony-probe.network]', 'enabled = false', '']
    (home/'config.toml').write_text('\n'.join(lines), encoding='utf-8')
    return home


def _credential_targets():
    done = subprocess.run(['cmdkey', '/list'], capture_output=True, check=False, timeout=60)
    return sum(1 for line in done.stdout.splitlines() if b':' in line and b'arget' in line)


def test_codex_file_auth_is_not_reachable_from_the_mxc_sandbox(tmp_path):
    git = shutil.which('git')
    api, run = BoundedApi(32), uuid.uuid4().hex[:12]
    decoy_key = 'sk-decoy-'+secrets.token_hex(16)
    policy_value = 'decoy-'+secrets.token_hex(16)
    proof = {'run': run, 'codex': str(CODEX)}
    proof['host_before'] = dict(_host_state(git), credential_targets=_credential_targets())
    clients = []
    try:
        attempt = tmp_path/'attempt'
        attempt.mkdir()
        python = _probe_python(tmp_path)
        plan_dir = tmp_path/'plan'
        plan_dir.mkdir()
        home = _auth_home(tmp_path, attempt, (python.parent, plan_dir))
        client_home = tmp_path/'client-home'
        for sub in ('', 'appdata', 'local', 'temp'):
            (client_home/sub).mkdir(parents=True, exist_ok=True)
        env = _client_environment(home, client_home, policy_value)
        env.pop('OPENAI_API_KEY')  # the key lives only in the file
        env[POLICY_DECOY] = policy_value
        proof['client_secret_like_names'] = sorted(n for n in env if 'KEY' in n.upper() or 'TOKEN' in n.upper())
        # --- the decoy key goes into the file through the documented login path -------------------
        login = subprocess.run([str(CODEX), 'login', '--with-api-key'], input=decoy_key.encode(), env=env,
                               capture_output=True, timeout=120, check=False)
        status = subprocess.run([str(CODEX), 'login', 'status'], env=env, capture_output=True, timeout=120, check=False)
        auth = home/'auth.json'
        stored = json.loads(auth.read_text(encoding='utf-8')) if auth.exists() else {}
        proof['login'] = {'exit': login.returncode, 'status_exit': status.returncode,
                          'status': status.stdout.decode('utf-8', 'replace').replace(decoy_key, '<decoy>')[-500:],
                          'file_holds_decoy': stored.get('OPENAI_API_KEY') == decoy_key,
                          'auth_mode': stored.get('auth_mode')}
        plan = {'attempt': str(attempt), 'auth': str(auth), 'home': str(home), 'config': str(home/'config.toml')}
        plan_file = plan_dir/'plan.json'
        plan_file.write_text(json.dumps(plan), encoding='utf-8')
        # --- the probe in the sandbox ---------------------------------------------------------------
        client = Client(api, [str(CODEX), 'sandbox', '-C', str(attempt), '-c', 'windows.sandbox="mxc"', '-P',
                              'symphony-probe', '--', str(python), '-I', '-B', '-c', AUTH_PROBE, str(plan_file)],
                        attempt, env, 'SymphonyMxcAuth-'+run, tmp_path, home)
        clients.append(client)
        report = attempt/'report.json'
        try:
            wait_until(report.exists, timeout=180, what='auth probe report')
        except AssertionError:
            pass  # recorded as a missing report and asserted after the proof is saved
        proof['client'] = client.finish(30)
        proof['client']['stopped'] = client.close()
        clients.remove(client)
        proof['output'] = client.output()
        probe = json.loads(report.read_text(encoding='utf-8')) if report.exists() else None
        digests = {'decoy_key': hashlib.sha256(decoy_key.encode()).hexdigest(),
                   'policy_decoy': hashlib.sha256(policy_value.encode()).hexdigest()}
        if probe:
            env_step = probe['steps'][0]
            env_step['key_value_visible'] = digests['decoy_key'] in env_step.get('value_digests', [])
            env_step['policy_decoy_value_visible'] = digests['policy_decoy'] in env_step.get('value_digests', [])
            env_step['policy_decoy_name_visible'] = POLICY_DECOY in env_step.get('names', [])
            env_step.pop('value_digests', None)
            env_step['names'] = [n for n in env_step.get('names', []) if 'KEY' in n.upper() or 'TOKEN' in n.upper()]
        proof['probe'] = probe
        proof['decoy_in_output'] = any(decoy_key in (text or '') for text in proof['output'].values())
        proof['owner_reads_auth'] = auth.exists() and bool(auth.read_bytes())
    finally:
        for client in clients:
            try:
                client.close()
            except Exception as exc:  # health: allow BLE001 -- the proof records every cleanup failure
                proof.setdefault('cleanup_errors', []).append(repr(exc))
        proof['host_after'] = dict(_host_state(git), credential_targets=_credential_targets())
        save_proof('codex-file-auth', proof)

    # --- verdicts, after the proof is saved ---------------------------------------------------------
    assert proof['host_after'] == proof['host_before'], 'host or credential store changed'
    assert proof['login']['exit'] == 0 and proof['login']['file_holds_decoy'], 'file auth not stored as documented'
    assert 'API key configured' in proof['login']['status'], 'client does not see the file credential'
    assert proof['owner_reads_auth'] and proof['decoy_in_output'] is False
    probe = proof['probe']
    assert probe is not None and [s['name'] for s in probe['steps']] == EXPECTED
    steps = {s['name']: s for s in probe['steps']}
    assert steps['a1-env']['key_value_visible'] is False, 'the stored key reached the command environment'
    for name in ('a2-read-auth', 'a3-list-home', 'a4-read-config'):
        assert steps[name]['ok'] is False, (name, steps[name])
    # policy_decoy_* is recorded only: it shows whether `codex sandbox` applies shell_environment_policy
