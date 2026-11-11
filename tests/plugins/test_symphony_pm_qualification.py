"""Fixture contracts for the qualification record. The dummy executable is only ever hashed.

No process, Job object, driver, CLI or model is started; this does not qualify a real build.
"""
from dataclasses import asdict, replace
import hashlib
import os
from types import SimpleNamespace

import pytest

from hermes_cli.plugin_request_binding import canonical_home
from plugins.symphony_pm.transport.codex_launcher import CodexLauncher, PreparedCodexAttempt
from plugins.symphony_pm.transport.qualification import QualificationRecord, environment_digest
from plugins.symphony_pm.transport.types import ExecutionPlan, LaunchTicket, Refused

BYTES, KIT, ENV, NS, SCHED = b'not-a-real-binary', 'a'*40, 'env-1', 'qual-ns', 'sched-1'
ENVIRONMENT = {'K': 'v', 'PATH': 'c:/tools'}
windows = pytest.mark.platforms('windows')


def make_record(tmp_path, **changes):
    exe = tmp_path/'codex.exe'
    exe.write_bytes(BYTES)
    exe.chmod(0o755)
    base = QualificationRecord(canonical_home(tmp_path), NS, SCHED, KIT, str(exe),
                               hashlib.sha256(BYTES).hexdigest(), ENV, environment_digest(ENVIRONMENT),
                               ('issuer-1', 'issuer-2'))
    return replace(base, **changes)


@pytest.fixture
def record(tmp_path):
    return make_record(tmp_path)


class FakeDriver:
    runtime, model, effort, timeout, required_features = 'codex', 'model-1', 'high', 5, ()

    def __init__(self, target):
        self._target, self.runner, self.launches, self.cleanups = target, None, 0, 0
        self.probed = SimpleNamespace(readiness='ready', require=lambda *features: None)

    def implementation_argv(self, pack, prompt):
        return ['exec']

    def reject_bypass(self, argv):
        pass

    def launch_implementation(self, pack, prompt):
        self.launches += 1
        return {'returncode': 0, 'timedOut': False}

    def collect_result(self, pack):
        return {'status': 'done'}

    def cleanup(self, pack):
        self.cleanups += 1


def attempt(tmp_path, q, *, target=None, kit_revision=None, environment_ref=None):
    workspace = tmp_path/'work'
    workspace.mkdir(exist_ok=True)
    env = environment_ref or q.environment_ref
    plan = ExecutionPlan('model-1', 'high', env, str(workspace), 'b'*40)
    ticket = LaunchTicket(q.namespace, 'project-1', 'job-1', 'rev-1', 'attempt-1', 'c'*64, 'permit-1', plan, 'session-1')
    runner = SimpleNamespace(owner=SimpleNamespace(
        scope=SimpleNamespace(namespace=q.namespace, profile_home=q.profile_home)))
    pack = SimpleNamespace(worktree=str(workspace), checkpoint_sha='b'*40, peer_runtime='codex', peer_spawn_allowed=False)
    driver = FakeDriver(target or q.executable)
    prepared = PreparedCodexAttempt(driver, pack, 'prompt', env, lambda result: None, kit_revision or q.kit_revision)
    return CodexLauncher(lambda t, r: prepared, qualification=q), ticket, runner, driver


def test_valid_record_passes_with_stable_digest(record):
    record.validate()
    assert record.digest == replace(record).digest and len(record.digest) == 64
    assert record.digest != replace(record, kit_revision='d'*40).digest


@pytest.mark.parametrize('field,value', [
    ('profile_home', 'relative/home'), ('profile_home', 7), ('namespace', ''), ('namespace', ' bad'),
    ('scheduler_id', 'bad id'), ('environment_ref', ''), ('kit_revision', 'a'*39), ('kit_revision', 'A'*40),
    ('executable_sha256', 'a'*63), ('executable_sha256', 'G'*64),
    ('environment_sha256', 'a'*63), ('environment_sha256', 'G'*64), ('environment_sha256', None), ('executable', 'codex.exe'),
    ('executable', 5), ('issuer_refs', ()), ('issuer_refs', ['issuer-1']), ('issuer_refs', ('i-1', 'i-1')),
    ('issuer_refs', ('bad id',)), ('issuer_refs', (1,)),
])
def test_every_structural_defect_refuses(tmp_path, field, value):
    with pytest.raises(Refused, match='QUALIFICATION_RECORD'):
        make_record(tmp_path, **{field: value}).validate()


def test_noncanonical_home_and_non_exe_refuse(tmp_path):
    with pytest.raises(Refused, match='QUALIFICATION_RECORD'):
        make_record(tmp_path, profile_home=str(tmp_path/'sub'/'..')).validate()
    with pytest.raises(Refused, match='QUALIFICATION_RECORD'):
        make_record(tmp_path, executable=str(tmp_path/'codex.bin')).validate()
    make_record(tmp_path, executable=str(tmp_path/'CODEX.EXE')).validate()


@windows
def test_verify_executable_accepts_the_qualified_bytes(record):
    record.verify_executable()


@windows
def test_verify_executable_refuses_missing_and_changed_files(record, tmp_path):
    (tmp_path/'codex.exe').write_bytes(BYTES + b'x')
    with pytest.raises(Refused, match='QUALIFIED_EXECUTABLE_CHANGED'):
        record.verify_executable()
    (tmp_path/'codex.exe').unlink()
    with pytest.raises(Refused, match='QUALIFIED_EXECUTABLE_CHANGED'):
        record.verify_executable()


def test_launcher_takes_binding_from_record_and_rejects_non_records(tmp_path, record):
    launcher, *_ = attempt(tmp_path, record)
    assert (launcher.profile_home, launcher.namespace, launcher.qualification) == (
        record.profile_home, record.namespace, record)
    for bad in (None, asdict(record), SimpleNamespace(**asdict(record))):
        with pytest.raises(Refused, match='QUALIFICATION_RECORD'):
            CodexLauncher(lambda t, r: None, qualification=bad)
    with pytest.raises(Refused, match='QUALIFICATION_RECORD'):
        CodexLauncher(lambda t, r: None, qualification=replace(record, namespace=''))


@windows
def test_launcher_runs_a_matching_attempt(tmp_path, record):
    launcher, ticket, runner, driver = attempt(tmp_path, record)
    assert launcher(ticket, object(), runner) == {'status': 'done'}
    assert (driver.launches, driver.cleanups, driver.runner) == (1, 1, runner)


@pytest.mark.parametrize('case', ['kit_revision', 'environment_ref', 'executable_path'])
def test_launcher_refuses_attempt_disagreeing_with_record(tmp_path, record, case):
    other = tmp_path/'other.exe'
    other.write_bytes(BYTES)
    other.chmod(0o755)
    kwargs = {'kit_revision': {'kit_revision': 'd'*40}, 'environment_ref': {'environment_ref': 'env-2'},
              'executable_path': {'target': str(other)}}[case]
    launcher, ticket, runner, driver = attempt(tmp_path, record, **kwargs)
    with pytest.raises(Refused, match='QUALIFIED_CODEX_ATTEMPT_REQUIRED'):
        launcher(ticket, object(), runner)
    assert (driver.launches, driver.cleanups, driver.runner) == (0, 0, None)


@windows
def test_launcher_refuses_binary_changed_after_qualification(tmp_path, record):
    launcher, ticket, runner, driver = attempt(tmp_path, record)
    (tmp_path/'codex.exe').write_bytes(BYTES + b'swapped')
    with pytest.raises(Refused, match='QUALIFIED_EXECUTABLE_CHANGED'):
        launcher(ticket, object(), runner)
    assert (driver.launches, driver.cleanups, driver.runner) == (0, 0, None)


def test_environment_digest_binds_values_not_insertion_order():
    assert environment_digest({'A': '1', 'B': '2'}) == environment_digest({'B': '2', 'A': '1'})
    assert environment_digest({'A': '1', 'B': '2'}) != environment_digest({'A': '1', 'B': '3'})
    assert environment_digest({'A': '1'}) != environment_digest({'A': '1', 'B': ''})


def denied(action):
    try:
        action()
    except OSError as error:
        return error
    return None


def write_attempts(path, other):
    """Overwrite, replace and delete the file; the first OSError per attempt, None when it succeeded."""
    def overwrite():
        with open(path, 'r+b') as stream:
            stream.write(b'x')
    return (denied(overwrite), denied(lambda: os.replace(other, path)), denied(lambda: os.unlink(path)))


@windows
def test_held_executable_denies_write_replace_and_delete_until_released(tmp_path, record):
    path, other = tmp_path/'codex.exe', tmp_path/'swap.exe'
    other.write_bytes(b'replacement')
    with record.held_executable():
        assert all(isinstance(error, OSError) for error in write_attempts(path, other))
        with open(path, 'rb') as reader:  # the image loader only reads, so readers must stay compatible
            assert reader.read() == BYTES
    assert write_attempts(path, other) == (None, None, None)


@windows
def test_held_executable_releases_on_error_and_refuses_changed_bytes(tmp_path, record):
    path, other = tmp_path/'codex.exe', tmp_path/'swap.exe'
    other.write_bytes(b'replacement')
    with pytest.raises(RuntimeError):
        with record.held_executable():
            raise RuntimeError('boom')
    assert denied(lambda: os.replace(other, path)) is None
    with pytest.raises(Refused, match='QUALIFIED_EXECUTABLE_CHANGED'):
        with replace(record, executable_sha256='0'*64).held_executable():
            pytest.fail('a mismatching hash must not enter the block')
    assert denied(lambda: os.unlink(path)) is None  # a refusal does not leave the file held


@windows
def test_launcher_holds_the_executable_until_the_launch_returns(tmp_path, record):
    launcher, ticket, runner, driver = attempt(tmp_path, record)
    other = tmp_path/'swap.exe'
    other.write_bytes(b'replacement')
    seen = []

    def launch(pack, prompt):
        seen.append(write_attempts(record.executable, other))
        return {'returncode': 0, 'timedOut': False}
    driver.launch_implementation = launch
    assert launcher(ticket, object(), runner) == {'status': 'done'}
    assert len(seen) == 1 and all(isinstance(error, OSError) for error in seen[0])
    assert write_attempts(record.executable, other) == (None, None, None)


def host_kwargs(tmp_path, q, **changes):
    launcher, *_ = attempt(tmp_path, q)
    kwargs = dict(profile_home=tmp_path, namespace=q.namespace, launcher=launcher, environment=dict(ENVIRONMENT),
                  command_result=object(), sanitize=str, approval_transport=object(), scheduler_id=q.scheduler_id,
                  controller='controller-1', trusted_issuers=list(q.issuer_refs), qualification=q)
    return {**kwargs, **changes}


@windows
def test_matching_declaration_assembles_without_creating_anything(tmp_path, record):
    from plugins.symphony_pm.transport.bootstrap import TransportBootstrap
    from plugins.symphony_pm.transport.windows_bootstrap import windows_transport
    kwargs = host_kwargs(tmp_path, record)
    before = sorted(p.name for p in tmp_path.iterdir())
    bootstrap = windows_transport(**kwargs)
    assert type(bootstrap) is TransportBootstrap
    assert bootstrap.trusted_issuers == record.issuer_refs and bootstrap.launcher.qualification == record
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@windows
@pytest.mark.parametrize('case', ['profile', 'namespace', 'scheduler', 'issuers', 'issuer_order', 'launcher_record',
                                  'environment_value', 'environment_extra_key'])
def test_declaration_refuses_each_single_mismatch(tmp_path, record, case):
    from plugins.symphony_pm.transport.windows_bootstrap import windows_transport
    changes = {'profile': {'profile_home': tmp_path/'other-home'}, 'namespace': {'namespace': 'other-ns'},
               'scheduler': {'scheduler_id': 'sched-2'}, 'issuers': {'trusted_issuers': ['issuer-1']},
               'issuer_order': {'trusted_issuers': ['issuer-2', 'issuer-1']},
               'environment_value': {'environment': {**ENVIRONMENT, 'PATH': 'c:/elsewhere'}},
               'environment_extra_key': {'environment': {**ENVIRONMENT, 'CODEX_HOME': 'c:/other'}}}.get(case, {})
    kwargs = host_kwargs(tmp_path, record, **changes)
    if case == 'launcher_record':
        kwargs['launcher'] = attempt(tmp_path, replace(record, kit_revision='d'*40))[0]
    with pytest.raises(Refused, match='QUALIFICATION_BINDING'):
        windows_transport(**kwargs)


@windows
def test_declaration_refuses_non_record_invalid_record_and_changed_binary(tmp_path, record):
    from plugins.symphony_pm.transport.windows_bootstrap import windows_transport
    kwargs = host_kwargs(tmp_path, record)
    for bad in (None, asdict(record), SimpleNamespace(**asdict(record))):
        with pytest.raises(Refused, match='QUALIFICATION_RECORD'):
            windows_transport(**{**kwargs, 'qualification': bad})
    with pytest.raises(Refused, match='QUALIFICATION_RECORD'):
        windows_transport(**{**kwargs, 'qualification': replace(record, issuer_refs=())})
    (tmp_path/'codex.exe').write_bytes(BYTES + b'swapped')
    with pytest.raises(Refused, match='QUALIFIED_EXECUTABLE_CHANGED'):
        windows_transport(**kwargs)
