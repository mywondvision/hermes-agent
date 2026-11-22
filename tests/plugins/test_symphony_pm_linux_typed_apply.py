"""Public Linux ingress, typed owner decisions, checked capture and Robot ref recovery, offline."""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event
import time
from types import SimpleNamespace

import pytest

from gateway.platforms.event import MessageType
from plugins.symphony_pm.contracts import LaunchExpired
from plugins.symphony_pm.status_cli import read_launches
from plugins.symphony_pm.transport import linux_sandbox, robot_apply_git
from plugins.symphony_pm.transport.bootstrap import serve_owner
from plugins.symphony_pm.transport.check_command import CheckCommand, DIFF_CHECK
from plugins.symphony_pm.transport.linux_check_host import LinuxCheckTerminal
from plugins.symphony_pm.transport.linux_robot_check import LinuxGitChecks, LinuxGitCheckHost
from plugins.symphony_pm.transport.linux_launcher import LinuxLauncher
from plugins.symphony_pm.transport.linux_lifecycle import LinuxLifecycleOwner
from plugins.symphony_pm.transport.types import Refused, digest
from tests.plugins.symphony_typed_human_support import human_session
from tests.plugins.test_symphony_pm_codex_exec import _git
from tests.plugins.test_symphony_pm_linux_lifecycle import Guard, Native, Observer, rig
from tests.plugins.symphony_serving_support import approve as record_permit
from tests.plugins.test_symphony_pm_linux_owner_check import checker_stand
from tests.plugins.test_symphony_pm_typed_approval import displayed


PROJECT_TEST = """from pathlib import Path
import subprocess
def test_checked_candidate():
    assert subprocess.check_output(['git', 'show', 'HEAD:task.txt']).strip() == EXPECTED
    assert not subprocess.check_output(['git', 'remote']).strip()
    assert subprocess.run(['git', 'config', '--get', 'symphony.owner-only']).returncode == 1
    assert not Path('.git/objects/info/alternates').exists()
    assert not Path('owner-notes.txt').exists()
"""


class FixtureGitHost(LinuxGitCheckHost):
    def __init__(self, pin):
        self.pin, self.calls = pin, []

    def attested_profile(self):
        return self.pin

    def start(self, invocation, *, timeout):
        clone = Path(invocation.directory)
        assert _git('rev-parse', 'HEAD', cwd=clone) == invocation.commit
        assert _git('rev-parse', 'HEAD^', cwd=clone) == invocation.source_sha
        env = {k: v for k, v in os.environ.items() if k.upper() in
               {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATHEXT'}}
        env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        self.process = subprocess.Popen([sys.executable, *invocation.command.arguments[:-1],
                                         '-p', 'no:cacheprovider', '--', invocation.command.arguments[-1]],
                                        cwd=clone, env=env, stdin=subprocess.DEVNULL)
        self.calls.append(invocation)

    def observe(self, invocation, *, timeout):
        assert self.calls[-1] == invocation
        code = self.process.wait(timeout=timeout)
        return LinuxCheckTerminal(invocation.key, self.pin, invocation.epoch, code, False, 'd'*64)


@pytest.fixture
def linux_chat(tmp_path_factory, monkeypatch, checker_stand, request):
    return make_linux_chat(tmp_path_factory, monkeypatch, checker_stand, getattr(request, 'param', 'pass'))


def make_linux_chat(tmp_path_factory, monkeypatch, checker_stand, mode='pass'):
    tmp_path = tmp_path_factory.mktemp('chat')
    project_test = PROJECT_TEST.replace('EXPECTED', repr(b'different' if mode == 'fail' else b'changed'))
    stand = checker_stand('good', tmp_path/'profile', 'symphony_pm:ingress-linux', clone=True, project_test=project_test)
    launcher, repo = stand.launcher, stand.repo
    _git('branch', 'fixture-base', stand.source, cwd=repo)
    _git('config', 'symphony.owner-only', 'fixture', cwd=repo)
    (repo/'owner-notes.txt').write_bytes(b'private notes')
    config_file = tmp_path/'runtime/etc/symphony/codex/config.toml'
    config_file.parent.mkdir(parents=True)
    config_file.write_bytes(b'model="fixture-only"\nmodel_reasoning_effort="low"\n')
    q = launcher.qualification
    q.hashes = ('0'*64,)*6 + (hashlib.sha256(config_file.read_bytes()).hexdigest(),)
    q.settings_sha256 = digest(asdict(launcher.settings))
    q.runtime, q.environment_ref = str(tmp_path/'runtime'), 'fixture-linux'
    checker = launcher.owner_check
    project_command = CheckCommand('project-git', 'python', ('-I', '-B', '-m', 'pytest', 'test_revision.py'), True)
    primary = () if mode == 'deferred' else checker.profile.commands
    profile = replace(checker.profile, commands=(*primary, DIFF_CHECK, project_command))
    launcher.owner_check = replace(checker, profile=profile)
    checker.host.profile = profile
    # The stand attests a test executable on the host; the byte digest still runs.
    monkeypatch.setattr(linux_sandbox, 'protected', lambda path: Path(path))
    evidence = '3'*64
    pin = digest(dict(kind='linux-git-check-v1', primary_profile=profile.fingerprint, evidence_sha256=evidence))
    git_host = FixtureGitHost(pin)
    launcher.git_checks = LinuxGitChecks(evidence, git_host)
    class LiveClockObserver(Observer):
        def handshake(self, *args):
            self.now[0] = time.time()
            return super().handshake(*args)

        def read(self, *args):
            self.now[0] = time.time()
            return super().read(*args)
    observer = LiveClockObserver([time.time()])
    class FinishingNative(Native):
        def create_suspended(self, *args):
            receiver = super().create_suspended(*args)
            wait = receiver.wait_result
            def wait_result(*values, **options):
                observer.stopped = True
                return wait(*values, **options)
            receiver.wait_result = wait_result
            return receiver
    native = FinishingNative()
    finished = Event()
    failures = []
    class Owner(LinuxLifecycleOwner):
        def execute_reserved(self, *args):
            try:
                return super().execute_reserved(*args)
            except Exception as exc:
                failures.append(exc)
                raise
            finally:
                finished.set()

    generation = 0
    def session(restart=False, typed_enabled=True, desktop=False, desktop_request_id=None):
        nonlocal generation
        generation += 1
        def factory(scope, approvals):
            guard = Guard(scope, f'fixture-{generation}', restart)
            owner = Owner(scope, guard, observer, native, launcher, scheduler_id='serving-test', controller='fixture')
            scope.on_close(owner.close)
            return serve_owner(owner, approvals, ('issuer',))
        return human_session(tmp_path, restart=restart, project_ref=launcher.settings.project_ref,
                             launcher=launcher, repository=repo, linux_factory=factory, typed_enabled=typed_enabled,
                             desktop=desktop, desktop_request_id=desktop_request_id)
    return stand, profile, native, finished, failures, session, mode


@pytest.mark.parametrize('linux_chat', ['pass', 'fail', 'unknown'], indirect=True)
def test_linux_typed_plan_checked_patch_and_lost_apply_receipt(linux_chat, monkeypatch, record_property):
    stand, profile, native, finished, failures, session, mode = linux_chat
    launcher, repo = stand.launcher, stand.repo
    q, git_host = launcher.qualification, launcher.git_checks.host

    with session() as channel:
        _, deny = displayed(channel, 'deny')
        assert 'DENIED' in channel.deliver(deny)
        assert native.created == [] and not read_launches(channel.owner.journal.path)
        plan, approve = displayed(channel)
        assert (plan['source_sha'], plan['configured_model'], plan['configured_effort']) == (
            stand.source, 'fixture-only', 'low')
        assert plan['check_policy_sha256'] == profile.fingerprint
        assert plan['git_check_profile'] == git_host.pin
        assert channel.deliver(approve, message_type=MessageType.VOICE) == 'ORIGINAL_TYPED_COMMAND_REQUIRED'
        assert not native.created
        assert 'APPROVED' in channel.deliver(approve)
        assert not native.created
        assert channel._submit('approve') == 'SUBMITTED'
        assert finished.wait(20)
        assert not failures, failures
        row, = read_launches(channel.owner.journal.path)
        assert row['state'] == 'STOPPED' and row['stop_proof'] == 'CGROUP_EMPTY'
        assert row['result']['check']['commands'] == {'value': 0}
        patch = row['result']['patch']['sha256']
        job = row['job']
        ticket, _ = channel.owner.journal.read_launch(
            channel.owner.token, launcher.settings.project_ref, job, row['revision'])
        # Inspect the production argv without invoking it. The fixture worker above only writes test bytes.
        q.permission_profile, q.executable, q.probe_python = 'fixture', '/image/codex', '/image/python'
        call = launcher._call(ticket, SimpleNamespace(goal='fixture', raw_constraints=()),
                              SimpleNamespace(src=launcher.active/'src'), {})
        assert call.argv[-5:] == ('-c', 'model="fixture-only"', '-c', 'model_reasoning_effort="low"', '-')
        changed = json.loads(ticket.plan.typed_details)
        changed['configured_effort'] = 'different'
        changed_ticket = replace(ticket, plan=replace(ticket.plan, typed_details=json.dumps(changed)))
        with pytest.raises(Refused, match='TYPED_EXECUTOR_CHANGED'):
            LinuxLauncher.__call__(launcher, changed_ticket, None, SimpleNamespace(owner=channel.owner))
        ref = 'refs/heads/symphony/' + job
        command = f'/symphony apply {job} {patch}'
        assert not _git('for-each-ref', '--format=%(refname)', ref, cwd=repo)
    with session(restart=True) as channel:
        recapture = '/symphony recapture ' + job
        archived_task = stand.attempts/ticket.attempt/'src'/'task.txt'
        archived_bytes = archived_task.read_bytes()
        archived_task.write_bytes(b'other revision')
        assert channel.deliver(recapture) == 'LINUX_ARCHIVE_CHANGED'
        archived_task.write_bytes(archived_bytes)
        rebuilt = json.loads(channel.deliver(recapture))
        assert rebuilt['patch_sha256'] == patch
        saved, = read_launches(channel.owner.journal.path)
        latest = next(o for o in saved['observations'] if o['seq'] == rebuilt['seq'])
        Path(latest['body']['patch']['path']).write_bytes(b'different fixture bytes')
        assert channel.deliver(command) == 'ROBOT_PATCH_DIGEST'
        rebuilt = json.loads(channel.deliver(recapture))
        assert rebuilt['patch_sha256'] == patch
        assert channel.deliver(command, message_type=MessageType.VOICE) == 'ORIGINAL_TYPED_COMMAND_REQUIRED'
        assert channel.deliver(f'/symphony apply {job} ' + '0'*64) == 'ROBOT_CAPTURE_IDENTITY'
        assert not _git('for-each-ref', '--format=%(refname)', ref, cwd=repo)
        if mode == 'unknown':
            observe = git_host.observe
            def lost_terminal(invocation, *, timeout):
                observe(invocation, timeout=timeout)
            monkeypatch.setattr(git_host, 'observe', lost_terminal)
            assert channel.deliver(command) == 'ROBOT_CHECK_INCOMPLETE'
            invocation, = git_host.calls
            marker = Path(launcher.settings.state)/'robot-git-check.active'/'invocation.json'
            assert digest(json.loads(marker.read_bytes())) == invocation.key
            assert channel.deliver(command) == 'CHECK_STOP_UNPROVEN'
            assert channel.deliver(command+' --accept-failed-git-checks') == 'ROBOT_PRESENTED_FAILURE_AND_FULL_DIGEST_REQUIRED'
            assert len(git_host.calls) == 1 and not _git('for-each-ref', '--format=%(refname)', ref, cwd=repo)
            record_property('linux_offline', dict(state='CHECK_INCOMPLETE', invocation=invocation.key,
                            invocation_count=1, ref_writes=0))
            return
        publish = robot_apply_git.publish_prepared
        operations = []
        def lost(*args, readback_only=False, **kwargs):
            operations.append(readback_only)
            result = publish(*args, readback_only=readback_only, **kwargs)
            if not readback_only:
                raise Refused('ROBOT_REF_OUTCOME_UNKNOWN')
            return result
        monkeypatch.setattr(robot_apply_git, 'publish_prepared', lost)
        assert not git_host.calls
        if mode == 'fail':
            assert channel.deliver(command+' --accept-failed-git-checks') == 'ROBOT_PRESENTED_FAILURE_AND_FULL_DIGEST_REQUIRED'
            failure = json.loads(channel.deliver(command))
            assert failure['state'] == 'CHECK_FAILED_SHOWN' and failure['git_checks'][-1]['exit_code'] == 1
            assert not operations and not _git('for-each-ref', '--format=%(refname)', ref, cwd=repo)
            command += ' --accept-failed-git-checks'
        assert channel.deliver(command) == 'ROBOT_REF_OUTCOME_UNKNOWN'
        oid = _git('rev-parse', ref, cwd=repo)
        assert len(git_host.calls) == 1 and not Path(git_host.calls[0].directory).exists()
    with session(restart=True) as recovered:
        result = json.loads(recovered.deliver(command))
        assert result['state'] == 'APPLIED' and result['commit'] == oid
        assert result['identity']['capture_seq'] < rebuilt['seq']
        check, project = result['git_checks']
        assert check == dict(name=DIFF_CHECK.name, exit_code=0, source_sha=stand.source, candidate_sha=oid)
        assert (project['source_sha'], project['candidate_sha'], project['invocation'], project['exit_code']) == (
            stand.source, oid, git_host.calls[0].key, int(mode == 'fail'))
        assert result['accepted_failed_git_checks'] == (mode == 'fail')
        assert operations == [False, True]
    assert _git('show', '-s', '--format=%P|%an|%ae', oid, cwd=repo) == (
        f'{stand.source}|Robot|robot@symphony.invalid')
    assert _git('show', oid+':task.txt', cwd=repo) == 'changed'
    assert _git('rev-parse', 'HEAD', cwd=repo) == stand.source
    assert (repo/'task.txt').read_bytes() == b'owner dirty'
    assert (repo/'owner-notes.txt').read_bytes() == b'private notes'
    assert (repo/'.git/index').read_bytes() == stand.before
    record_property('linux_offline', dict(source=stand.source, patch_sha256=patch, commit=oid,
                    ref=ref, stop_proof=row['stop_proof'], state_after_restart=result['state'],
                    git_checks=result['git_checks'], ref_writes=operations.count(False),
                    recovery_reads=operations.count(True), sandbox_checks=row['result']['check']['commands']))


@pytest.mark.parametrize('linux_chat', ['deferred'], indirect=True)
def test_all_deferred_profile_cannot_display_a_plan_or_reserve_an_attempt(linux_chat, record_property):
    stand, _, native, _, _, session, _ = linux_chat
    with session() as channel:
        with pytest.raises(Refused, match='CHECK_NO_COMMANDS_EXECUTED'):
            stand.launcher.typed_plan()
        assert channel.request('approve') == ('WAITING_FOR_AUTHORITY', '')
        assert not channel.owner.typed_approval.slots
        with sqlite3.connect(channel.owner.journal.path.as_uri()+'?mode=ro', uri=True) as db:
            assert db.execute('SELECT COUNT(*) FROM serving_permits').fetchone() == (0,)
        assert not read_launches(channel.owner.journal.path) and not native.created
        record_property('linux_offline', dict(primary_commands=0, displayed=0, permits=0, reservations=0))


def test_git_project_check_requires_current_qualification_at_plan_time(linux_chat, monkeypatch):
    stand, _, native, *_ = linux_chat
    launcher = stand.launcher
    monkeypatch.setattr(launcher.git_checks.host, 'pin', 'e'*64)
    with pytest.raises(Refused, match='LINUX_GIT_CHECK_QUALIFICATION_CHANGED'):
        launcher.typed_plan()
    monkeypatch.setattr(launcher, 'git_checks', None)
    with pytest.raises(Refused, match='LINUX_GIT_CHECK_NOT_QUALIFIED'):
        launcher.typed_plan()
    assert not native.created


def test_legacy_linux_capture_stays_readable_without_typed_apply(linux_chat):
    stand, _, _, _, _, session, _ = linux_chat
    with session(typed_enabled=False) as channel:
        owner = channel.owner
        request, context, _ = record_permit(SimpleNamespace(owner=owner, scope=owner.scope, now=[owner.clock()]))
        ticket, _ = owner.submit(request, context)
    with session(restart=True) as channel:
        owner = channel.owner
        view = json.loads(channel.deliver('/symphony status ' + ticket.job))
        assert view['view']['phase'] == 'COMPLETED'
        before = read_launches(owner.journal.path)
        patch = before[0]['result']['patch']['sha256']
        assert channel.deliver(f'/symphony apply {ticket.job} {patch}') == 'LINUX_TYPED_PLAN_REQUIRED'
        assert read_launches(owner.journal.path) == before
        assert not _git('for-each-ref', '--format=%(refname)', 'refs/heads/symphony', cwd=stand.repo)
        assert json.loads(channel.deliver('/symphony status ' + ticket.job + ' --saved'))['view'] == view['view']


def test_expired_suspended_linux_unit_remains_held_until_cgroup_proof(tmp_path, monkeypatch, record_property):
    stand = rig(tmp_path)
    owner = stand.owner
    try:
        request, context, permit = record_permit(stand)
        ticket, _, _ = owner.journal.reserve_launch(owner.token, request, context, stand.now[0])
        create = stand.native.create_suspended
        def create_at_expiry(*args):
            receiver = create(*args)
            stand.now[0] = permit.expires_at
            return receiver
        monkeypatch.setattr(stand.native, 'create_suspended', create_at_expiry)
        with pytest.raises(LaunchExpired):
            owner.execute_reserved(owner.token, ticket, request)
        assert stand.native.created[0].resumes == 0
        def current():
            return owner.journal.read_launch(owner.token, ticket.project, ticket.job, ticket.revision)[1]
        assert current()['state'] == 'HELD_UNKNOWN' and not current()['stop_proof']
        owner.reconcile()
        assert current()['state'] == 'HELD_UNKNOWN'
        stand.observer.stopped = True
        owner.reconcile()
        assert owner.journal.proven_stop(current()) and current()['stop_proof'] == 'CGROUP_EMPTY'
        record_property('linux_offline', dict(resumes=stand.native.created[0].resumes,
                        state_before_proof='HELD_UNKNOWN', state_after_proof=current()['state'],
                        proof=current()['stop_proof']))
    finally:
        owner.close(0)
