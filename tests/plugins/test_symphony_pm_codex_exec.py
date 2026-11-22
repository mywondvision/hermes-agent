"""A1/A2 fixture: submit -> CodexExecLauncher -> journal -> `hermes symphony status`, with a stand-in client.

The stand-in Codex is this interpreter running a script; the native provider is an in-process
fixture that starts it with subprocess. Everything else is production code: the lifecycle owner,
journal, permit check, attempt copy, sandbox precheck verdict, capture, owner-side check and the
read-only status command. No model, no sandbox and no Job is started here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli.plugin_request_binding import RequestIdentity, canonical_home
from hermes_platform.resolver import locate_command
from plugins.symphony_pm import adapter as pm
from plugins.symphony_pm import status_cli
from plugins.symphony_pm.transport.codex_client import command_outcome
from plugins.symphony_pm.transport.codex_exec_launcher import (
    CONFIG_SELECTED, AttemptSettings, ClientJobCoverage, CodexExecLauncher, CodexExecQualification, client_environment,
    file_sha256,
)
from plugins.symphony_pm.transport.lifecycle import LifecycleOwner
from plugins.symphony_pm.transport.qualification import environment_digest
from plugins.symphony_pm.transport.types import ExecutionPlan, NativeBinding, Permit, ProcessIdentity, Refused
from tests.plugins.symphony_serving_support import Guard, Observer
from tests.plugins.symphony_owner_check_support import owner_client
from tests.plugins.test_symphony_pm_adapter import ctx, submit_payload

pytestmark = pytest.mark.platforms('windows')  # the qualified executable is held through a Windows handle

FAKE_CODEX = r'''
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
mode = os.environ['FAKE_CODEX_MODE']
if args[0] == 'sandbox':
    command = args[args.index('--')+1:]
    report = Path(command[5])
    names = ['SYSTEMROOT', 'SYSTEMDRIVE', 'TEMP', 'TMP']
    steps = [{'name': 'env', 'ok': True, 'names': names, 'name_count': len(names)}]
    for name in ('read-auth', 'list-codex-home', 'read-outside', 'write-outside', 'loopback'):
        steps.append({'name': name, 'ok': mode == 'leaky' and name == 'read-outside', 'errno': 13})
    report.write_text(json.dumps({'mode': 'precheck', 'steps': steps}), encoding='utf-8')
    sys.exit(0)
assert args[0] == 'exec'
prompt = sys.stdin.read()
src = Path(args[args.index('-C')+1])
Path(args[args.index('-o')+1]).write_text('done', encoding='utf-8')
(src.parent/'exec-ran').write_text(prompt, encoding='utf-8')
value = '3' if mode == 'wrong' else '2'
(src/'pkg'/'value.py').write_text('VALUE = '+value+'\n', encoding='utf-8', newline='\n')
(src/'pkg'/'__pycache__').mkdir()
for event in ({'type': 'thread.started'}, {'type': 'item.started', 'item': {'type': 'command_execution'}},
              {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}}):
    print(json.dumps(event))
'''


class FixtureReceiver:
    def __init__(self, ticket, argv, cwd, stdin_text, environment):
        root = ProcessIdentity(300, 'fixture-client-birth')
        self.binding = NativeBinding(ticket, 'fixture-job-'+ticket.attempt, root, (root,))
        self.argv, self.cwd, self.stdin, self.environment = list(argv), cwd, stdin_text, environment
        self.process = None

    def resume(self, _stdin, *, authorize):
        def start():
            self.process = subprocess.Popen(self.argv, cwd=self.cwd, env=self.environment, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        authorize(start)

    def wait_result(self, argv, timeout, timed_out=False):
        out, err = self.process.communicate(self.stdin.encode('utf-8'), timeout=timeout)
        return command_outcome(argv, self.process.returncode, stdout=out.decode(), stderr=err.decode(),
                               timed_out=timed_out)

    def check_binding(self, binding):
        assert binding == self.binding

    def close_fail_safe(self, _deadline):
        return None


class FixtureNative:
    ownership_contract = 'fixture-contained-process'
    def __init__(self, launcher):
        self.launcher, self.created, self.unbound = launcher, [], []

    def validate_driver_call(self, ticket, argv, cwd, timeout, stdin_text):
        self.launcher.validate_call(ticket, tuple(argv), cwd, timeout, stdin_text)

    def create_suspended(self, ticket, argv, cwd, stdin_text):
        receiver = FixtureReceiver(ticket, argv, cwd, stdin_text, dict(self.launcher.environment))
        self.created.append(receiver)
        return receiver

    def run_unbound(self, argv, cwd, timeout, streams):
        self.unbound.append(list(argv))
        done = subprocess.run(argv, cwd=cwd, env=dict(self.launcher.environment), stdin=streams[0],
                              stdout=streams[1], stderr=streams[2], timeout=timeout, check=False)
        return done.returncode, False


def _git(*args, cwd):
    environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='fixture',
                       GIT_AUTHOR_EMAIL='fixture@example.invalid', GIT_COMMITTER_NAME='fixture',
                       GIT_COMMITTER_EMAIL='fixture@example.invalid')
    return subprocess.run([locate_command('git').command[0], *args], cwd=cwd, env=environment, check=True,
                          capture_output=True).stdout.decode().strip()


def _repository(root):
    root.mkdir()
    (root/'pkg').mkdir()
    (root/'pkg'/'__init__.py').write_bytes(b'')
    (root/'pkg'/'value.py').write_bytes(b'VALUE = 1\n')
    _git('init', '-q', cwd=root)
    _git('add', '-A', cwd=root)
    _git('commit', '-q', '-m', 'fixture', cwd=root)
    return _git('rev-parse', 'HEAD', cwd=root)


def rig(tmp_path, mode='clean'):
    home, codex_home, attempts = tmp_path/'profile', tmp_path/'codex-home', tmp_path/'attempts'
    for directory in (home, codex_home, attempts):
        directory.mkdir()
    (codex_home/'config.toml').write_text('default_permissions = "symphony-pilot"\n', encoding='utf-8')
    source = _repository(tmp_path/'repo')
    fake = tmp_path/'fake_codex.py'
    fake.write_text(FAKE_CODEX, encoding='utf-8')
    executable = str(Path(sys.executable).resolve())
    environment = {k: v for k, v in os.environ.items() if k.upper() in ('SYSTEMROOT', 'PATH', 'WINDIR', 'TEMP', 'TMP')}
    environment['FAKE_CODEX_MODE'] = mode
    scope = SimpleNamespace(profile_home=canonical_home(home), namespace='symphony_pm:ingress', active=True)
    exehash, confighash = file_sha256(executable), file_sha256(codex_home/'config.toml')
    coverage = ClientJobCoverage(Guard.recovery_host_id, exehash, confighash, 'symphony-pilot', 'f'*64)
    q = CodexExecQualification(scope.profile_home, scope.namespace, 'serving-test', executable,
                               exehash, str(codex_home), confighash, 'symphony-pilot', 'fixture-client',
                               environment_digest(environment), ('issuer',), coverage)
    checks = lambda paths: (('value', [sys.executable, '-c', 'import pkg.value as v; assert v.VALUE == 2']),)
    settings = AttemptSettings(str(tmp_path/'repo'), str(attempts), sys.executable, checks, dict(os.environ),
                               command_prefix=(str(fake),))
    launcher = CodexExecLauncher(q, settings, environment=environment)
    native, now = FixtureNative(launcher), [1000.0]
    observer = Observer(now)
    observer.stopped = True  # the stand-in has completed before the launcher asks for stop evidence
    owner = LifecycleOwner(scope, Guard(scope), observer, native, launcher, scheduler_id='serving-test',
                           controller='controller-1', clock=lambda: now[0])
    return SimpleNamespace(owner=owner, native=native, scope=scope, now=now, source=source, home=home,
                           attempts=attempts, launcher=launcher)


def submit(r, *, workspace=None, request_id='req-1', reserve_only=False):
    payload = submit_payload(request_id=request_id)
    request, _, _ = pm.parse_intent(payload)
    identity = RequestIdentity(r.scope.profile_home, r.scope.namespace, 'session-1', request.request_id,
                               request.payload_digest)
    plan = ExecutionPlan(CONFIG_SELECTED, CONFIG_SELECTED, 'fixture-client',
                         workspace or str(r.attempts/'active'/'src'), r.source)
    permit = Permit('permit-'+request.request_id, identity, request.project_ref, request.request_digest,
                    'b'*64, 'policy', 'scope', 'issuer', r.now[0]+600, plan)
    r.owner.record_permit(r.owner.token, permit, 'fixture-approval', 'c'*64, r.now[0])
    context = ctx(payload, issuer_ref='issuer', policy_ref='policy', grant_ref=permit.ref,
                  approved_scope_revision='scope', contract_digest='b'*64, scheduler_id='serving-test',
                  scheduler_epoch=r.owner.token.epoch, now=r.now[0], expires_at=permit.expires_at)
    if reserve_only:
        return r.owner.journal.reserve_launch(r.owner.token, request, context, r.now[0])
    return r.owner.submit(request, context)


def launches(r):
    return [r.owner.journal.read_launch(r.owner.token, t.project, t.job, t.revision)[1]
            for t in (receiver.binding.ticket for receiver in r.native.created)]


def status(home, monkeypatch, capsys):
    """The command through the same attach function `hermes` uses for plugin commands."""
    from hermes_cli.main import _attach_plugin_cli_command
    registered = {}
    status_cli.register(SimpleNamespace(register_cli_command=lambda **entry: registered.update(entry)))
    parser = argparse.ArgumentParser(prog='hermes')
    _attach_plugin_cli_command(parser.add_subparsers(dest='command'), registered)
    args = parser.parse_args(['symphony', 'status', '--json'])
    monkeypatch.setenv('HERMES_HOME', str(home))
    assert args.func(args) == 0
    return json.loads(capsys.readouterr().out)


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob('*')):
        digest.update(str(path.relative_to(root)).encode())
        if path.is_file():
            digest.update(path.read_bytes())
            digest.update(str(path.stat().st_mtime_ns).encode())
    return digest.hexdigest()


def test_full_path_records_patch_check_and_status_reads_it_without_writing(tmp_path, monkeypatch, capsys):
    r = rig(tmp_path)
    with owner_client(r):
        ticket, fresh = submit(r)
    assert fresh and len(r.native.unbound) == 1 and len(r.native.created) == 1
    assert r.native.unbound[0][2:4] == ['sandbox', '-C']
    [row] = launches(r)
    result = json.loads(row['result'])
    assert (row['state'], row['stop_proof']) == ('STOPPED', 'JOB_EMPTY')
    assert result['stop'] is None and result['precheck']['verdict'] == 'PROBE_OK'
    patch = Path(result['patch']['path'])
    assert result['patch']['paths'] == ['pkg/value.py']
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == result['patch']['sha256']
    assert result['cleaned'] == ['pkg/__pycache__']
    check = result['check']
    assert (check['apply'], check['commands'], check['ok']) == (0, {'value': 0}, True)
    assert (check['attempt'], check['patch_sha256']) == (ticket.attempt, result['patch']['sha256'])
    assert result['usage'] == [{'input_tokens': 10, 'output_tokens': 2}] and result['commands'] == 1
    assert result['inexec_probe'] == 'PROBE_NOT_RUN'  # observed only; the stand-in never runs it
    assert result['workspace'] == {'path': str(r.attempts/ticket.attempt), 'renamed': True}
    assert not (r.attempts/'active').exists()
    assert (r.attempts/ticket.attempt/'exec-ran').read_text(encoding='utf-8').rstrip().endswith(
        'never delete branches.\n--- end of task ---')
    journal_dir = Path(r.owner.journal.path).parent
    before = tree_digest(journal_dir)
    shown = status(r.home, monkeypatch, capsys)
    assert tree_digest(journal_dir) == before
    [launch] = shown['launches']
    assert (launch['attempt'], launch['patch_sha256'], launch['check_ok'], launch['precheck']) == (
        ticket.attempt, result['patch']['sha256'], True, 'PROBE_OK')


def test_precheck_refusal_spends_the_permit_without_starting_the_model(tmp_path):
    r = rig(tmp_path, mode='leaky')
    with pytest.raises(Refused, match='CODEX_PRECHECK_REFUSED'):
        submit(r)
    assert len(r.native.unbound) == 1 and r.native.created == []
    assert launches(r) == []
    ticket, fresh = submit(r)
    _, row = r.owner.journal.read_launch(r.owner.token, ticket.project, ticket.job, ticket.revision)
    assert row['state'] == 'NOT_STARTED' and row['binding'] is None and row['result'] is None
    [kept] = [p for p in r.attempts.iterdir()]
    assert kept.name == row['attempt'] and not (kept/'exec-ran').exists()
    assert not fresh and ticket.attempt == row['attempt'] and len(r.native.unbound) == 1


def test_failed_owner_check_is_recorded_not_raised(tmp_path):
    r = rig(tmp_path, mode='wrong')
    with owner_client(r):
        submit(r)
    [row] = launches(r)
    result = json.loads(row['result'])
    check = result['check']
    assert (check['apply'], check['commands'], check['ok']) == (0, {'value': 1}, False)
    assert check['reason'] == 'CHECK_COMMAND_FAILED'


def test_plan_outside_the_active_attempt_is_refused_before_anything_is_created(tmp_path):
    r = rig(tmp_path)
    with pytest.raises(Refused, match='CODEX_EXEC_PLAN_BINDING'):
        submit(r, workspace=str(tmp_path/'elsewhere'))
    assert list(r.attempts.iterdir()) == [] and r.native.unbound == []


def test_changed_codex_config_is_refused(tmp_path):
    r = rig(tmp_path)
    (Path(r.launcher.qualification.codex_home)/'config.toml').write_text('model = "other"\n', encoding='utf-8')
    with pytest.raises(Refused, match='CODEX_CONFIG_CHANGED'):
        submit(r)
    assert list(r.attempts.iterdir()) == []


def test_status_without_journal_creates_nothing(tmp_path, monkeypatch, capsys):
    home = tmp_path/'empty-profile'
    home.mkdir()
    assert status(home, monkeypatch, capsys) == {'journal': False, 'launches': []}
    assert list(home.iterdir()) == []


def test_client_environment_keeps_only_allow_listed_names(tmp_path):
    """A credential need not be named like one: the first native run carried a `*_WEBHOOK` URL past a name filter."""
    source = {'PATH': 'C:\\Windows', 'SystemRoot': 'C:\\Windows', 'PROJECT_B24_WEBHOOK': 'https://x/rest/1/abc/',
              'CRM_SQL_READONLY': 'dsn', 'OPENAI_API_KEY': 'sk-x', 'APPDATA': 'C:\\elsewhere'}
    environment = client_environment(source, codex_home=tmp_path/'codex', client_home=tmp_path/'client')
    assert set(environment) == {'PATH', 'SystemRoot', 'SYSTEMDRIVE', 'CODEX_HOME', 'USERPROFILE', 'HOME', 'APPDATA',
                                'LOCALAPPDATA', 'TEMP', 'TMP'}
    assert environment['APPDATA'] == str(tmp_path/'client'/'appdata')
