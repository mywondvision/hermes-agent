"""Offline owner-check through LinuxLauncher._run, LinuxLifecycleOwner and the real v2 journal."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli.plugin_request_binding import canonical_home
from plugins.symphony_pm import status_cli
from plugins.symphony_pm.transport import linux_check_host as ch, linux_control, linux_workspace as lw
from plugins.symphony_pm.transport.check_command import CheckCommand
from plugins.symphony_pm.transport.linux_check import check_marker
from plugins.symphony_pm.transport.linux_launcher import LinuxLauncher, LinuxSettings
from plugins.symphony_pm.transport.recapture import attempt_data
from plugins.symphony_pm.transport.types import ExecutionPlan, Refused
from tests.plugins import symphony_serving_support as f
from tests.plugins.test_symphony_pm_linux_lifecycle import Guard, rig, state, submit


class FixtureHost(ch.LinuxCheckHost):
    def __init__(self, profile, fault):
        self.profile, self.fault = profile, fault
        self.started, self.reads = [], []
        self.pending = fault in ('timeout', 'wrong-terminal')

    def attested_profile(self):
        if self.fault == 'stale' or self.fault == 'replaced' and self.started:
            return 'e' * 64
        return self.profile.fingerprint

    def start(self, invocation, *, timeout):
        self.started.append(invocation)
        assert invocation.command in self.profile.commands and not invocation.command.requires_git
        assert timeout > 0
        src = Path(invocation.directory)
        assert (src/'task.txt').read_bytes() == b'changed\n'
        assert not (src/'.git').exists() and not (src/'local.txt').exists()
        if self.fault == 'copy-change':
            (src/'task.txt').write_bytes(b'altered\n')

    def observe(self, invocation, *, timeout):
        self.reads.append(invocation)
        if self.pending and self.fault == 'timeout' and timeout:
            raise TimeoutError('fixture observation')
        result = ch.LinuxCheckTerminal(invocation.key, invocation.profile, invocation.epoch,
                                       0, False, 'c' * 64)
        if self.pending:
            return replace(result, invocation='0' * 64)
        return result


class OfflineLauncher(LinuxLauncher):
    """Only the sandbox/model side is synthetic. Create, finish, capture, check and record normally."""
    def admission(self):
        self.require_check_slot_clear()

    def __call__(self, ticket, request, runner):
        self.active.mkdir()
        _, workspace, capture = lw.prepare(self, ticket)
        (workspace.src/'task.txt').write_bytes(b'changed\n')
        call = SimpleNamespace(argv=('fixture', 'exec'), workspace=ticket.plan.workspace, timeout=1, prompt='fixture')
        return self._run(ticket, runner, capture, call)


@pytest.fixture
def checker_stand(tmp_path_factory, monkeypatch):
    return make_checker_stand(tmp_path_factory, monkeypatch)


def make_checker_stand(tmp_path_factory, monkeypatch):
    def build(fault, home=None, namespace='symphony-linux', *, clone=False, project_test=None):
        base = tmp_path_factory.mktemp('lc')
        repo, attempts, state_dir, scratch = (base/name for name in ('r', 'a', 's', 'c'))
        for directory in (repo, attempts, state_dir, scratch):
            directory.mkdir()
        git = Path(lw._git()).resolve(strict=True)
        def run(*args):
            return subprocess.run([str(git), '-C', str(repo), *args], check=True, capture_output=True).stdout
        run('init', '-q')
        (repo/'task.txt').write_bytes(b'base\n')
        run('add', 'task.txt')
        if project_test is not None:
            (repo/'test_revision.py').write_text(project_test, encoding='utf8')
            run('add', 'test_revision.py')
        run('-c', 'user.name=Checker', '-c', 'user.email=fixture@invalid', 'commit', '-q', '-m', 'source')
        if clone:
            destination = base/'clone'
            run('clone', '--no-local', str(repo), str(destination))
            repo = destination
        source = run('rev-parse', 'HEAD').decode().strip()
        (repo/'local.txt').write_bytes(b'owner staged')
        run('add', 'local.txt')
        (repo/'task.txt').write_bytes(b'owner dirty')
        before = (repo/'.git'/'index').read_bytes()
        settings = LinuxSettings('proj-alpha', str(repo), str(attempts), str(state_dir), 1, 1, 1, 1, 1)
        home = canonical_home(home or base/'p')
        q = SimpleNamespace(namespace=namespace, profile_home=home, host_id=Guard.recovery_host_id,
                            validate=lambda: None, verify=lambda: None, fingerprint='2'*64)
        ordinary = CheckCommand('value', 'python', ('-I', '-B', '-m', 'fixture_value'))
        git_test = CheckCommand('git-test', 'python', ('-I', '-m', 'fixture_git'), True)
        commands = (git_test,) if fault == 'all-git' else (ordinary, git_test) if fault == 'mixed' else (ordinary,)
        if fault == 'two':
            commands = (ordinary, CheckCommand('lint', 'ruff', ('check', 'task.txt')))
        profile = ch.LinuxCheckProfile(q.fingerprint, '1'*64, str(scratch), str(git),
                                       hashlib.sha256(git.read_bytes()).hexdigest(), commands, 30)
        host = FixtureHost(profile, fault)
        launcher = OfflineLauncher(q, settings, owner_check=None if fault == 'absent' else ch.LinuxOwnerCheck(profile, host))
        # The stand has no Linux kernel mount table, protected Linux binaries or directory-fsync API.
        monkeypatch.setattr(lw, '_mountpoints', lambda: set())
        monkeypatch.setattr(lw, '_sync_directory', lambda directory: None)
        monkeypatch.setattr(linux_control, '_sync_directory', lambda directory: None)
        monkeypatch.setattr(ch, 'protected', lambda path: Path(path))
        monkeypatch.setattr(ch, 'private_directory', lambda path: Path(path))
        monkeypatch.setattr(f, 'ExecutionPlan', lambda *_: ExecutionPlan('fixture', 'high', 'fixture', str(attempts/'active'/'src'), source))
        if fault == 'tamper':
            capture = lw.capture_patch
            def changed(*args):
                patch = capture(*args)
                Path(patch['path']).write_bytes(b'tampered')
                return patch
            monkeypatch.setattr(lw, 'capture_patch', changed)
        return SimpleNamespace(home=home, repo=repo, before=before, attempts=attempts, scratch=scratch,
                               source=source, launcher=launcher, checker_host=host)
    return build


@pytest.fixture
def stand(checker_stand):
    def build(fault):
        cfg = checker_stand(fault)
        launcher, home, repo = cfg.launcher, cfg.home, cfg.repo
        r = rig(Path(home), stopped=True, launcher=launcher)
        ticket = submit(r, 'first')
        row = state(r, ticket)
        result = json.loads(row['result'])
        entries = status_cli.read_launches(r.owner.journal.path)[0]['observations']
        capture_row, check_row = (next(e for e in entries if e['kind'] == kind) for kind in ('CAPTURE', 'CHECK'))
        assert check_row['body']['capture_ref'] == {'attempt': ticket.attempt, 'seq': capture_row['seq'],
                                                   'sha256': result['patch']['sha256']}
        assert result['check']['source_sha'] == cfg.source and check_row['body']['check'] == result['check']
        assert (repo/'.git'/'index').read_bytes() == cfg.before
        assert (repo/'task.txt').read_bytes() == b'owner dirty' and (repo/'local.txt').read_bytes() == b'owner staged'
        assert (cfg.attempts/ticket.attempt/'src'/'task.txt').read_bytes() == b'changed\n'
        assert Path(result['patch']['path']).is_file()
        return r, launcher, ticket, cfg.checker_host, result, cfg.scratch
    return build


@pytest.mark.parametrize(('fault', 'reason', 'starts'), [
    ('good', None, 1), ('absent', 'QUALIFIED_CHECKER_REQUIRED', 0),
    ('stale', 'CHECK_QUALIFICATION_CHANGED', 0), ('replaced', 'CHECK_QUALIFICATION_CHANGED', 1),
    ('tamper', 'CHECK_PATCH_CHANGED', 0), ('copy-change', 'CHECK_COPY_CHANGED', 1),
    ('all-git', 'CHECK_NO_COMMANDS_EXECUTED', 0), ('mixed', 'CHECK_GIT_TESTS_DEFERRED', 1),
])
def test_check_primary_result(stand, fault, reason, starts):
    r, launcher, ticket, host, result, scratch = stand(fault)
    check = result['check']
    assert check['ok'] is (reason is None) and check.get('reason') == reason
    assert len(host.started) == starts
    if fault in ('good', 'mixed'):
        assert check['commands'] == {'value': 0} and check['stop_receipts'] == {'value': 'c'*64}
    if fault in ('mixed', 'all-git'):
        assert check['deferred'] == ['git-test']
    assert check_marker(launcher).exists() == (fault == 'replaced')
    assert bool(list(scratch.iterdir())) == (fault == 'replaced')
    assert state(r, ticket)['stop_proof'] == 'CGROUP_EMPTY'


@pytest.mark.parametrize(('fault', 'reason'), [('timeout', 'CHECK_TIMEOUT'), ('wrong-terminal', 'CHECK_STOP_UNPROVEN')])
def test_uncertain_check_survives_restart_and_only_observes(stand, fault, reason):
    r, launcher, ticket, host, result, scratch = stand(fault)
    assert result['check']['reason'] == reason and result['check']['execution_hold']
    assert check_marker(launcher).exists() and list(scratch.iterdir())
    durable = state(r, ticket)['result']
    successor = rig(Path(r.scope.profile_home), predecessor=True, stopped=True, launcher=launcher)
    with pytest.raises(Refused, match='CHECK_STOP_UNPROVEN'):
        submit(successor, 'second')
    assert state(successor, ticket)['result'] == durable and len(host.started) == 1
    host.pending = False
    successor.owner.reconcile()
    assert state(successor, ticket)['result'] == durable
    assert not check_marker(launcher).exists() and not list(scratch.iterdir())
    assert len(host.started) == 1 and host.reads[-1] == host.started[0]


@pytest.mark.parametrize('conflict', [False, True])
def test_restart_after_storing_exact_terminal_does_not_query_again(stand, conflict):
    r, launcher, ticket, host, result, scratch = stand('timeout')
    invocation = host.started[0]
    data = attempt_data(launcher.qualification, ticket)
    terminal = ch.LinuxCheckTerminal(invocation.key, invocation.profile, invocation.epoch+int(conflict), 0, False, 'c'*64)
    linux_control.publish(data, 'check-stop-' + invocation.nonce + '.json', asdict(terminal))
    before = tuple(host.reads)
    successor = rig(Path(r.scope.profile_home), predecessor=True, stopped=True, launcher=launcher)
    assert tuple(host.reads) == before and len(host.started) == 1
    assert check_marker(launcher).exists() == conflict and bool(list(scratch.iterdir())) == conflict
    assert json.loads(state(successor, ticket)['result']) == result

@pytest.mark.parametrize('cut', ['before-delete', 'after-delete', 'replaced-directory'])
def test_durable_stop_precedes_private_copy_deletion_and_slot_release(stand, monkeypatch, cut):
    from plugins.symphony_pm.transport import linux_check
    dispose = linux_check._dispose
    def interrupt_cleanup(directory, profile, identity):
        if cut == 'after-delete':
            dispose(directory, profile, identity)
        raise OSError('fixture interruption before the owner can release the slot')
    monkeypatch.setattr(linux_check, '_dispose', interrupt_cleanup)
    r, launcher, ticket, host, primary, scratch = stand('good')
    invocation, = host.started
    directory = Path(invocation.directory).parent
    marker = check_marker(launcher)
    assert marker.exists() and primary['check']['ok'] is False
    data = attempt_data(launcher.qualification, ticket)
    receipt = ch.LinuxCheckTerminal(**json.loads((data/('check-stop-'+invocation.nonce+'.json')).read_bytes()))
    assert receipt.matches(invocation) and directory.exists() == (cut != 'after-delete')
    reads = tuple(host.reads)
    unrelated = scratch/'c-unrelated'
    unrelated.mkdir()
    (unrelated/'keep').write_bytes(b'foreign')
    monkeypatch.setattr(linux_check, '_dispose', dispose)
    if cut == 'replaced-directory':
        saved = scratch/'saved-with-original-identity'
        directory.rename(saved)
        directory.mkdir()
        (directory/'keep').write_bytes(b'other attempt')
    successor = rig(Path(r.scope.profile_home), predecessor=True, stopped=True, launcher=launcher)
    if cut == 'replaced-directory':
        assert marker.exists() and (directory/'keep').read_bytes() == b'other attempt'
        directory.rename(scratch/'foreign-replacement')
        saved.rename(directory)
        successor.owner.reconcile()
    assert not marker.exists() and not directory.exists()
    assert (unrelated/'keep').read_bytes() == b'foreign'
    assert tuple(host.reads) == reads and len(host.started) == 1
    assert json.loads(state(successor, ticket)['result']) == primary


def test_two_commands_each_retire_their_own_copy(stand):
    _, _, _, host, result, scratch = stand('two')
    first, second = host.started
    assert result['check']['ok'] and first.directory != second.directory
    assert first.tree_oid == second.tree_oid and not list(scratch.iterdir())


def test_saved_stage_requires_every_linux_receipt_and_current_qualification(stand):
    r, launcher, ticket, host, _result, _ = stand('two')
    journal, token = r.owner.journal, r.owner.token
    target = (ticket.project, ticket.job, ticket.revision)
    row, = status_cli.read_launches(journal.path)
    original = next(entry['body'] for entry in row['observations'] if entry['kind'] == 'CHECK')
    check = original['check']
    assert r.owner.observe_status(*target)['stage_acceptance'] == 'ACCEPTED'
    for change in (
            {'stop_receipts': {'value': 'c' * 64}},
            {'stop_receipts': {'value': 'c' * 64, 'lint': 'invalid'}},
            {'commands': {'value': 0, 'lint': True}},
            {'execution_hold': True},
            {'plan_sha256': 'b' * 64},
            {'revisions': {**check['revisions'], 'evidence': '9' * 64}},
            {'deferred': ['unspecified']}):
        journal.append_observation(token, ticket, {**original, 'check': {**check, **change}})
        assert r.owner.observe_status(*target)['stage_acceptance'] == 'NOT_ACCEPTED'
    journal.append_observation(token, ticket, original)
    launcher.owner_check = replace(launcher.owner_check, profile=replace(
        host.profile, commands=tuple(reversed(host.profile.commands))))
    assert r.owner.observe_status(*target)['stage_acceptance'] == 'NOT_ACCEPTED'
    launcher.owner_check = ch.LinuxOwnerCheck(host.profile, host)
    assert r.owner.observe_status(*target)['stage_acceptance'] == 'ACCEPTED'


def test_status_requires_independent_run_and_stop_records_for_each_command(stand):
    r, launcher, ticket, host, result, _ = stand('two')
    journal, token = r.owner.journal, r.owner.token
    args = ticket.project, ticket.job, ticket.revision
    capture = next(o for o in status_cli.read_launches(journal.path)[0]['observations'] if o['kind'] == 'CHECK')['body']
    check = capture['check']
    data = attempt_data(launcher.qualification, ticket)
    first, second = host.started
    def stage():
        return r.owner.observe_status(*args)['stage_acceptance']
    assert stage() == 'ACCEPTED'
    for updated in (
            {k: v for k, v in check.items() if k != 'runs'},
            {**check, 'runs': {**check['runs'], first.command.name: check['runs'][second.command.name]}},
            {**check, 'stop_receipts': {**check['stop_receipts'], first.command.name: 'f'*64}}):
        journal.append_observation(token, ticket, {**capture, 'check': updated})
        assert stage() == 'NOT_ACCEPTED'
    journal.append_observation(token, ticket, capture)
    for filename, key, bad in (('check-stop-', 'timed_out', True), ('check-stop-', 'returncode', 14),
                               ('check-run-', 'finished', 1e25), ('check-run-', 'terminal', 'e' * 64)):
        path = data/(filename + first.nonce + '.json')
        before = path.read_bytes()
        path.write_text(json.dumps({**json.loads(before), key: bad}))
        assert stage() == 'NOT_ACCEPTED'
        path.write_bytes(before)
    metadata = Path(result['patch']['path']).with_suffix('.json')
    before = metadata.read_bytes()
    metadata.write_text(json.dumps({**json.loads(before), 'after_tree': '0' * 40}))
    assert stage() == 'NOT_ACCEPTED'
    metadata.write_bytes(before)
    assert stage() == 'ACCEPTED'
    (data/('check-stop-' + first.nonce + '.json')).unlink()
    assert stage() == 'NOT_ACCEPTED'
