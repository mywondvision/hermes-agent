"""Opt-in, model-free Windows acceptance of a qualified owner check on a copy of A3's journal."""
import ctypes
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from threading import Event
import time
from types import SimpleNamespace

import pytest

from hermes_cli.plugin_request_binding import canonical_home
from plugins.symphony_pm.transport.attempt_workspace import Result
from plugins.symphony_pm.transport.codex_exec_check import check_patch
from plugins.symphony_pm.transport.codex_exec_launcher import AttemptSettings
from plugins.symphony_pm.transport.codex_exec_probe import _access_denied, _environment_ok
from plugins.symphony_pm.transport.journal import Journal
from plugins.symphony_pm.transport.owner_check_profile import CheckCommand, OwnerCheckProfile
from plugins.symphony_pm.transport.windows_ownership import WindowsOwnershipGuard

pytestmark = pytest.mark.platforms('windows')
SOURCE = '5fc59d649ad7dae8928cddd050979caa3b739e11'
PATCH = '2eb081977c1da688b5773f9c69b789771ace4a94abe9f2f90a830bfcc11d137c'
ATTEMPT = 'attempt-2c246d9242ed4c0b9580fdd395d668a7'
TEST = 'tests/plugins/test_symphony_pm_codex_exec_probe.py'
BOUNDARY = """
import json, os, sys
from pathlib import Path
directory = Path(sys.argv[1])
steps, error_types = [], {}
for name, mode in [('read.txt', 'rb'), ('write.txt', 'ab')]:
    step = {'name': 'read-outside' if mode == 'rb' else 'write-outside', 'ok': True, 'errno': None, 'winerror': None}
    try:
        with (directory/name).open(mode) as stream:
            if mode == 'ab':
                stream.write(b'CR3 synthetic write')
    except OSError as exc:
        step.update(ok=False, errno=exc.errno, winerror=getattr(exc, 'winerror', None))
        error_types[step['name']] = type(exc).__name__
    steps.append(step)
names = sorted(os.environ)
print('CR3_BOUNDARY '+json.dumps(dict(steps=steps, error_types=error_types, names=names, count=len(names))))
raise SystemExit(int(any(step['ok'] for step in steps)))
"""


def _hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _read(db, sql, *args):
    with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as conn:
        return conn.execute(sql, args).fetchall()


def _receipt(path, record):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def _denials(log):
    if not log.is_file():
        return None
    reports = [s.removeprefix('CR3_BOUNDARY ') for s in log.read_text('utf-8', errors='replace').splitlines()
               if s.startswith('CR3_BOUNDARY ')]
    if len(reports) != 1:
        return None
    try:
        obs = json.loads(reports[0])
        steps = obs['steps']
        accepted = ([s['name'] for s in steps] == ['read-outside', 'write-outside']
                    and all(_access_denied(s) for s in steps)
                    and _environment_ok({'name': 'env', 'ok': True, 'names': obs['names'], 'name_count': obs['count']}))
        return {'accepted': accepted, 'denials': steps, 'error_types': obs['error_types'],
                'environment_name_count': obs['count']}
    except (KeyError, TypeError, ValueError):
        return None


def _memory():
    status = ctypes.create_string_buffer(64)
    ctypes.c_uint32.from_buffer(status).value = len(status)
    deadline = time.monotonic()+300
    while True:
        assert ctypes.windll.kernel32.GlobalMemoryStatusEx(status)
        available = ctypes.c_uint64.from_buffer(status, 16).value
        if available >= int(1.1*(1 << 30)):
            return available
        assert time.monotonic() < deadline, 'native RAM gate'
        Event().wait(5)


def test_saved_patch_and_synthetic_controls_through_owner_mxc(monkeypatch):
    permit_dir = os.environ.get('HERMES_E2E_ARTIFACTS')
    permit = Path(permit_dir)/'cr3-owner-check.json' if permit_dir else None
    if not permit or not permit.is_file():
        pytest.skip('requires an explicit one-pass owner-check stand')
    spec = json.loads(permit.read_bytes())
    paths = {k: Path(v) for k, v in spec['paths'].items()}
    q = OwnerCheckProfile(**{k: paths[k] for k in ('root', 'client', 'python', 'ruff', 'git')}, **spec['pins'])
    home, controls, output = paths['home'], paths['controls'], permit.parent
    assert permit.resolve().parent == output and q.root.resolve() == q.root
    assert _hash(paths['patch']) == PATCH and _hash(q.home/'config.toml') == q.config_sha256
    before = {name: _hash(controls/name) for name in ('read.txt', 'write.txt')}
    assert before == spec['control_hashes'] and not q.active.exists()
    _receipt(output/'native.intent.json', {'permit_sha256': _hash(permit), 'available_bytes': _memory()})
    result = {'passed': False, 'source_sha': SOURCE, 'patch_sha256': PATCH, 'revisions': q.revisions(), 'runs': {}}
    run = subprocess.run
    result['client_start_free_bytes'] = []

    def observe(argv, *args, **kwargs):
        if argv[:2] == [str(q.client), 'sandbox']:
            result['client_start_free_bytes'].append(_memory())
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', observe)
    try:
        control_only = spec.get('control_only') is True
        _check_copies(q, home, controls, output, paths, result, control_only)
        assert not q.active.exists() and len(result['client_start_free_bytes']) == (1 if control_only else 3)
        result['passed'] = True
    finally:
        result['controls_equal'] = all(_hash(controls/name) == digest for name, digest in before.items())
        result['passed'] = result['passed'] and result['controls_equal']
        _receipt(output/'native.result.json', result)
        assert result['controls_equal']


def _check_copies(q, home, controls, output, paths, result, control_only):
    namespace = 'symphony_pm:ingress'
    scope = SimpleNamespace(profile_home=canonical_home(home), namespace=namespace)
    guard = WindowsOwnershipGuard(scope)
    try:
        journal = Journal(home, namespace, host_id=guard.recovery_host_id, workspace_clear=lambda: not q.active.exists())
        previous = journal.previous_owner()
        token = journal.claim(guard.identity, 'cr3-owner-check', previous, proof=guard.predecessor_proof(previous))
        [(job,)] = _read(journal.path, 'SELECT ticket FROM serving_launches WHERE attempt=?', ATTEMPT)
        fields = json.loads(job)
        ticket, row = journal.read_launch(token, fields['project'], fields['job'], fields['revision'])
        assert ticket.attempt == ATTEMPT and ticket.plan.source_sha == SOURCE and row['state'] == 'STOPPED'
        [(captured,)] = _read(journal.path, 'SELECT body FROM serving_observations WHERE attempt=? AND seq=1', ATTEMPT)
        capture = json.loads(captured)
        assert capture['kind'] == 'CAPTURE' and capture['source_sha'] == SOURCE and capture['patch']['sha256'] == PATCH
        assert capture['baseline_matches_source'] is True
        files = tuple(capture['patch']['paths'])
        assert files == ('plugins/symphony_pm/transport/codex_exec_launcher.py', TEST)
        patch = Result(paths['patch'], PATCH, files, True)
        run_pytest = ('-I', '-B', '-c', 'import os,pytest,sys; sys.path.insert(0,os.getcwd()); '
                      'raise SystemExit(pytest.main(sys.argv[1:]))', '-q', '-o', 'addopts=', '--noconftest',
                      '--disable-plugin-autoload', '-p', 'no:cacheprovider', TEST)
        plans = {
            'patch': (CheckCommand('ruff', 'ruff', ('check', '--no-cache', '--', *files)),
                      CheckCommand('pytest', 'python', run_pytest)),
            'controls': (CheckCommand('boundary', 'python', ('-I', '-B', '-c', BOUNDARY, str(controls))),),
        }
        ref = {'attempt': ATTEMPT, 'seq': 1, 'sha256': PATCH}
        if control_only:
            [positive, failure] = _read(journal.path, 'SELECT body FROM serving_observations WHERE attempt=? '
                                       'AND seq IN (2, 3) ORDER BY seq', ATTEMPT)
            completed, failed = (json.loads(r[0]) for r in (positive, failure))
            for prior in (completed, failed):
                assert prior['kind'] == 'CHECK' and prior['capture_ref'] == ref and prior['check']['source_sha'] == SOURCE
            assert completed['check']['ok'] is True and completed['check']['commands'] == {'ruff': 0, 'pytest': 0}
            assert failed['check']['ok'] is False and failed['check']['commands'] == {'boundary': 1}
            assert journal.next_observation_seq(token, ticket) == 4
            result['retained_check_seq'] = 2
            del plans['patch']
        settings = AttemptSettings(str(paths['repository']), str(paths['attempts']), str(q.python), lambda _: (),
                                   dict(os.environ, UNRELATED_CONTROL_FIELD='synthetic'), owner_check=q)
        for label, commands in plans.items():
            directory = output/label
            directory.mkdir()
            available = _memory()
            expected = journal.next_observation_seq(token, ticket)
            started = time.monotonic()
            check = check_patch(replace(settings, checks=lambda _, c=commands: c), str(q.git), directory,
                                patch, SOURCE, ATTEMPT)
            if label == 'controls':
                detail = _denials(directory/'check-boundary.log')
                if detail is not None:
                    check['boundary'] = detail
                if not detail or not detail['accepted']:
                    check.update(ok=False, reason=check.get('reason', 'CHECK_BOUNDARY_UNPROVEN'))
            event = {'kind': 'CHECK', 'capture_ref': ref, 'check': check}
            seq = journal.append_observation(token, ticket, event, expected_seq=expected)
            [(readback,)] = _read(journal.path, 'SELECT body FROM serving_observations WHERE attempt=? AND seq=?',
                                  ATTEMPT, seq)
            assert json.loads(readback) == event
            result['runs'][label] = {'check': check, 'capture_ref': ref, 'seq': seq, 'available_bytes': available,
                                     'seconds': round(time.monotonic()-started, 3)}
            _receipt(directory/'check.json', result['runs'][label])
            assert check['ok'] is True and check['apply'] == 0 and check['deferred'] == []
            assert check['commands'] == {c.name: 0 for c in commands}
            if label == 'patch':
                log = (directory/'check-pytest.log').read_text(encoding='utf-8', errors='replace')
                assert re.search(r'\b15 passed\b', log)
                result['runs'][label]['passed'] = 15
    finally:
        guard.close(0)
