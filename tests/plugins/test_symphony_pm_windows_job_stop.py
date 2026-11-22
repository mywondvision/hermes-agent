"""A model-free MXC campaign. The owning host supplies the pinned stand manifest explicitly."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli.approval_transport import RegisteredApprovalTransport
from hermes_cli.plugin_request_binding import canonical_home
from plugins.symphony_pm.transport.codex_exec_bootstrap import codex_exec_transport
from plugins.symphony_pm.transport.codex_exec_launcher import (
    AttemptSettings, CodexExecLauncher, CodexExecQualification, client_environment, file_sha256,
)
from plugins.symphony_pm.transport.qualification import environment_digest
from plugins.symphony_pm.transport.types import ProcessIdentity, Refused, job_object_name
from plugins.symphony_pm.transport.windows_api import WindowsApi
from tests.plugins.symphony_native_crash_support import save_proof
from tests.plugins.test_symphony_pm_codex_exec import submit
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')

# Both programs publish their own unrounded Windows creation time and have independent lifetimes.
# The parent-side witness checks the two reported identities without trusting their assertions of membership.
PROGRAM = '''\
import ctypes as C, json, os, subprocess, sys, threading, time
from ctypes import wintypes as W
from pathlib import Path
role, attempt = sys.argv[1:]
base = Path(__file__).parent
times = [W.FILETIME() for _ in range(4)]
if not C.windll.kernel32.GetProcessTimes(W.HANDLE(-1), *(C.byref(t) for t in times)):
    raise C.WinError()
identity = dict(pid=os.getpid(), creation=str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime))
temp = base/(role+'.tmp')
temp.write_text(json.dumps(dict(identity=identity, attempt=attempt)), encoding='utf-8')
temp.replace(base/(role+'.json'))
child = None
if role == 'payload':
    child = subprocess.Popen([sys.executable, '-I', '-B', __file__, 'child', attempt])
deadline = time.monotonic() + 45
while not (base/'release').exists() and time.monotonic() < deadline:
    threading.Event().wait(0.05)
if child is not None:
    child.wait(timeout=5)
'''


def _stand(tmp_path):
    artifacts = os.environ.get('HERMES_E2E_ARTIFACTS')
    if not artifacts or not (Path(artifacts)/'cr4-stand.json').is_file():
        pytest.skip('qualified CR-4 MXC campaign manifest required')
    config = json.loads((Path(artifacts)/'cr4-stand.json').read_text(encoding='utf-8'))
    for field in ('client', 'python', 'config'):
        assert file_sha256(config[field]) == config[field+'_sha256']
    attempts = Path(config['attempts_root'])
    assert attempts.is_dir() and not (attempts/'active').exists()
    client_home = tmp_path/'client'
    for sub in ('appdata', 'local', 'temp'):
        (client_home/sub).mkdir(parents=True)
    home = tmp_path/'owner'
    codex_home = Path(config['config']).parent
    env = client_environment(os.environ, codex_home=codex_home, client_home=client_home)
    closers = []
    scope = SimpleNamespace(profile_home=canonical_home(home), namespace='symphony_pm:cr4', active=True,
                            on_close=closers.append)
    q = CodexExecQualification(scope.profile_home, scope.namespace, 'serving-test', config['client'],
          config['client_sha256'], str(codex_home), config['config_sha256'], config['profile'], 'fixture-client',
          environment_digest(env), ('issuer',))
    settings = AttemptSettings(str(Path(__file__).resolve().parents[2]), str(attempts), config['python'],
                               lambda paths: (), {})
    launcher = CodexExecLauncher(q, settings, environment=env)
    transport = RegisteredApprovalTransport('test', lambda *_: pytest.fail('unexpected external request'),
                                           'fixture', scope.profile_home)
    bootstrap = codex_exec_transport(profile_home=scope.profile_home, namespace=scope.namespace, launcher=launcher,
        approval_transport=transport, scheduler_id=q.scheduler_id, controller='fixture-owner', trusted_issuers=('issuer',))
    port = bootstrap(scope).port
    return SimpleNamespace(launcher=launcher, owner=port.owner, port=port, now=[time.time()], scope=scope, attempts=attempts,
                           source=config['source_sha'], close=closers[0], config=config, qualified_ticket=None,
                           independent_stop=lambda: False)


def _attempt(stand, mode, ticket, runner, active):
    """Only this command can spend this fixture's launch ticket. No exec/model entry is bound."""
    launcher, q = stand.launcher, stand.launcher.qualification
    src = active/'src'
    src.mkdir()
    program = src/'job.py'
    program.write_text(PROGRAM, encoding='utf-8')
    argv = (q.executable, 'sandbox', '-C', str(src), '-P', q.permission_profile, '--', stand.config['python'],
            '-I', '-B', str(program), 'payload', ticket.attempt)
    timeout = 18 if mode == 'timeout' else 35
    with launcher.lock:
        launcher.commands[ticket.attempt] = (ticket, argv, str(src.resolve()), timeout, '')
    try:
        with q.held_executable():
            outcome = runner(list(argv), cwd=str(src), timeout=timeout, stdin_text='')
    finally:
        with launcher.lock:
            launcher.commands.pop(ticket.attempt)
        stopped, causes = runner.owner.finish_client_job(runner.token, ticket)
    workspace = launcher._settle(active, ticket.attempt) if stopped else {'path': str(active), 'renamed': False}
    diagnostics = [name for name in ('access is denied', 'not found', 'permission denied', 'modulenotfounderror', 'mxc')
                   if name in outcome.stderr.lower()]
    return {'exit_code': outcome.returncode, 'timed_out': outcome.timed_out, 'workspace': workspace, 'causes': causes,
            'stderr_sha256': hashlib.sha256(outcome.stderr.encode()).hexdigest(), 'diagnostics': diagnostics}


def _witness(api, binding, src, handles, records):
    expected = {'client': binding.root}
    for role in ('payload', 'child'):
        report = json.loads((src/(role+'.json')).read_text(encoding='utf-8'))
        assert report['attempt'] == binding.ticket.attempt
        expected[role] = ProcessIdentity(**report['identity'])
    job = api.open_job(job_object_name(binding.ticket))
    try:
        active, pids = api.snapshot(job)
        descendants, errors = set(), []
        # Include the authenticated payload tree even when a service parented it outside the client.
        for role in ('client', 'payload'):
            try:
                descendants.update(p.pid for p in psutil.Process(expected[role].pid).children(recursive=True))
            except psutil.Error as exc:
                errors.append({'role': role, 'error': type(exc).__name__})
        roles = {identity.pid: role for role, identity in expected.items()}
        for pid in sorted(set(pids) | descendants | set(roles)):
            role = roles.get(pid, 'member' if pid in pids else 'descendant')
            item = {'pid': pid, 'role': role, 'listed': pid in pids}
            records.append(item)
            try:
                handle = api.open_process(pid)
                handles[pid] = handle
                birth = api.creation(handle)
                identity = psutil.Process(pid)
                birth_seconds = (int(birth) - 116444736000000000) / 10_000_000
                independent_birth = abs(birth_seconds - identity.create_time()) < 0.00001
                item.update(creation=birth, in_job=api.in_job(handle, job), alive=not api.wait(handle),
                            expected_identity=(independent_birth and (pid not in roles or expected[role].creation == birth)))
                item.update(image=identity.name(), parent_pid=identity.ppid())
            except (OSError, psutil.Error) as exc:
                item.update(query_error=type(exc).__name__, winerror=getattr(exc, 'winerror', None))
        return {'active': active, 'pids': list(pids), 'descendant_pids': sorted(descendants), 'errors': errors,
                'reported': {role: asdict(identity) for role, identity in expected.items()}}
    finally:
        api.close(job)


def _observe_case(stand, mode, case, pool):
    api = WindowsApi()
    handles, records = {}, []
    src = stand.attempts/'active'/'src'
    stand.now[0] = time.time()
    future = pool.submit(submit, stand, request_id='cr4-'+mode)
    try:
        wait_until(lambda: (src/'child.json').exists() or future.done(), timeout=30, what='authenticated child barrier')
        if future.done():
            ticket, _ = future.result()
            _, row = stand.owner.journal.read_launch(stand.owner.token, ticket.project, ticket.job, ticket.revision)
            case.update(coverage='UNKNOWN', barrier='EARLY_EXIT', attempt=ticket.attempt, state=row['state'],
                        stop_proof=row['stop_proof'], hold_reason=row['hold_reason'], result=json.loads(row['result']))
            return
        with stand.owner.lock:
            receiver, = stand.owner._receivers.values()
            binding = receiver.binding
            case['attempt'] = binding.ticket.attempt
            case['processes'] = records
            try:
                case['before'] = _witness(api, binding, src, handles, records)
            except (OSError, Refused) as exc:
                case['before'] = {'errors': [type(exc).__name__], 'winerror': getattr(exc, 'winerror', None)}
            complete = (not case['before']['errors'] and records and case['before']['active'] == len(case['before']['pids'])
                        and all(item.get('expected_identity') and item.get('alive') and item['listed']
                                and item.get('in_job') for item in records))
            # The positive control covers only this synthetic ticket and lasts until its owner has settled it.
            # On UNKNOWN the production finalizer must close the Job and hold, even after a client-only zero.
            if complete:
                stand.qualified_ticket = binding.ticket
                stand.independent_stop = lambda: all(api.wait(handle) for handle in handles.values())
        trigger = time.time()
        if mode == 'exit':
            (src/'release').touch(exist_ok=False)
        elif mode == 'cancel':
            result = stand.owner.cancel_internal(binding, 'PM_CANCEL', request_id='bound-test-cancel')
            case['cancel'] = asdict(result)
        ticket, fresh = future.result(timeout=65)
        assert fresh and ticket == binding.ticket
        journal, token = stand.owner.journal, stand.owner.token
        _, row = journal.read_launch(token, ticket.project, ticket.job, ticket.revision)
        with sqlite3.connect(f'file:{journal.path.as_posix()}?mode=ro', uri=True) as db:
            saved = db.execute('SELECT body FROM serving_readbacks WHERE effect_key=?', (binding.effect_key,)).fetchone()
        effect = journal.effect(token, binding)
        if mode == 'timeout' and effect:
            trigger = effect['cancel_at']
        final = json.loads(saved[0]) if saved else None
        case.update(state=row['state'], stop_proof=row['stop_proof'], proof_source=row['proof_source'],
                    hold_reason=row['hold_reason'], result=json.loads(row['result']), readback=final,
                    seconds_to_readback=final['observed_at'] - trigger if final else None)
        for item in records:
            if item['pid'] in handles:
                item['signaled'] = api.wait(handles[item['pid']])
        case['coverage'] = 'VERIFIED' if complete and all(item.get('signaled') for item in records) else 'UNKNOWN'
    finally:
        # Only release our own marker after matching the recorded attempt to its live tree.
        if not future.done() and case.get('attempt') and (src/'payload.json').is_file():
            report = json.loads((src/'payload.json').read_text(encoding='utf-8'))
            if report['attempt'] == case['attempt']:
                (src/'release').touch()
        for handle in handles.values():
            api.close(handle)


def _available_memory(mode):
    started = time.monotonic()
    while (available := psutil.virtual_memory().available) < 1.1 * (1 << 30):
        save_proof('cr4-resource', {'stage': mode, 'available_bytes': available, 'waiting_for_memory': True})
        threading.Event().wait(3)
    return {'available_bytes': available, 'memory_wait_seconds': round(time.monotonic()-started, 3)}


def test_bound_mxc_jobs_stop_and_admit_a_new_permit_in_same_owner(tmp_path, monkeypatch):
    stand = _stand(tmp_path)
    proof = {'owner': asdict(stand.owner.guard.identity), 'cases': [], 'model_runs': 0}
    def qualified_fixture(guard):
        return (stand.qualified_ticket is not None and guard is stand.owner.guard
                and stand.qualified_ticket.attempt in stand.owner._receivers and stand.independent_stop())
    monkeypatch.setattr(stand.launcher, 'job_coverage_for', qualified_fixture)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            for mode in ('exit', 'timeout', 'cancel'):
                stand.qualified_ticket = None
                case = {'mode': mode, **_available_memory(mode)}
                proof['cases'].append(case)
                monkeypatch.setattr(stand.launcher, '_attempt', lambda git, ticket, request, runner, active:
                                    _attempt(stand, mode, ticket, runner, active))
                _observe_case(stand, mode, case, pool)
                if case['coverage'] != 'VERIFIED':
                    assert case['state'] == 'HELD_UNKNOWN' and case['hold_reason']
                    assert not case['result']['workspace']['renamed']
                    pytest.fail('D14 MXC membership or exact process stop unproven')
                assert case['state'] == 'STOPPED' and case['stop_proof'] == 'JOB_EMPTY'
                assert case['readback']['job_active'] == 0 and case['readback']['job_members'] == []
                assert case['result']['workspace']['renamed'] and not case['hold_reason']
                assert case['result']['timed_out'] == (mode == 'timeout')
                if mode == 'cancel':
                    assert case['cancel']['dispatched']
                assert case['coverage'] == 'VERIFIED', 'D14 MXC membership or exact process stop unproven'
        stand.now[0] = time.time()
        ticket, _, fresh = submit(stand, request_id='cr4-after-cancel', reserve_only=True)
        assert fresh and stand.owner.journal.mark_not_started(stand.owner.token, ticket)
        assert asdict(stand.owner.guard.identity) == proof['owner']
        proof['fresh_reservation_after_cancel'] = True
    finally:
        try:
            stand.close(time.monotonic()+5)
        finally:
            save_proof('cr4-windows-job-stop', proof)
