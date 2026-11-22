"""Portable contract tests for the Linux service, command qualification and one-use gates."""
from dataclasses import asdict, replace
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.symphony_pm.transport import linux_native
from plugins.symphony_pm.transport.linux_host import SystemdManager
from plugins.symphony_pm.transport.linux_identity import LinuxBinding, linux_binding_from_json, unit_for
from plugins.symphony_pm.transport.linux_launcher import LinuxLauncher, marker_targets
from plugins.symphony_pm.transport.linux_native import LinuxReceiver
from plugins.symphony_pm.transport.linux_sandbox import BOUNDARY_CHECKS, sandbox_command, verify_report
from plugins.symphony_pm.transport.types import ExecutionPlan, LaunchTicket, ProcessIdentity, Refused, digest


def identity():
    t = LaunchTicket('ns', 'project', 'job', '1'*64, 'attempt-'+'b'*32, '2'*64, 'permit',
                     ExecutionPlan('codex-config', 'codex-config', 'env', '/work', '3'*40), 'session')
    unit = unit_for(t)
    root = ProcessIdentity(41, '8013')
    return LinuxBinding(t, 'a'*64, 'b'*32, unit, 'c'*32, '/system.slice/'+unit+'/payload',
                        19, 31, root, (root,))


def test_saved_identity_binds_scope_group_and_original_root():
    bound = identity()
    assert linux_binding_from_json(asdict(bound)) == bound
    with pytest.raises(Refused):
        linux_binding_from_json({k: v for k, v in asdict(bound).items() if k != 'backend'})
    assert replace(bound, ticket=replace(bound.ticket, project='other')).effect_key != bound.effect_key
    for changed in (replace(bound, unit='arbitrary.service'), replace(bound, cgroup_path='/sys/payload'),
                    replace(bound, cgroup_inode=0), replace(bound, known_processes=()),
                    replace(bound, backend='unknown')):
        with pytest.raises(Refused):
            linux_binding_from_json(asdict(changed))


def test_each_marker_lives_at_its_own_protected_root(tmp_path):
    roots = {name: tmp_path/name for name in ('repo', 'state', 'profile', 'attempts', 'runtime')}
    namespace = 'qualified'
    settings = SimpleNamespace(repository=roots['repo'], state=roots['state'], attempts_root=roots['attempts'])
    q = SimpleNamespace(profile_home=str(roots['profile']), runtime=str(roots['runtime']), namespace=namespace)
    launcher = SimpleNamespace(settings=settings, qualification=q)
    marker = '.symphony-qualification-marker'
    content = b'symphony-qualification-marker\n'
    paths = {'deny_source': roots['repo'], 'deny_control': roots['state'],
             'deny_journal': roots['profile']/'plugin-data'/'symphony_pm'/digest(namespace)[:32],
             'deny_neighbor': roots['attempts']/'qualification-neighbor',
             'deny_credentials': roots['runtime']/'etc'/'symphony'/'codex'}
    expected = {}
    for key, root in paths.items():
        root.mkdir(parents=True)
        (root/marker).write_bytes(content)
        visible = '/etc/symphony/codex/' + marker if key == 'deny_credentials' else str(root/marker)
        expected[key] = (str(root/marker), visible, hashlib.sha256(content).hexdigest())
    q.marker_set_sha256 = digest(expected)
    targets = marker_targets(launcher)
    assert {targets[k] for k in expected} == {v[1] for v in expected.values()}
    (roots['repo']/marker).write_bytes(b'changed')
    with pytest.raises(Refused, match='LINUX_PROBE_MARKER'):
        marker_targets(launcher)


def test_client_stdin_preserves_approved_goal_and_ordered_constraints(tmp_path):
    q = SimpleNamespace(validate=lambda: None, executable='/client', probe_python='/python', permission_profile='p')
    launcher = LinuxLauncher(q, SimpleNamespace(exec_timeout=5, probe_timeout=2, project_ref='project'))
    launcher.require_project('project')
    with pytest.raises(Refused, match='LINUX_PROJECT_BINDING'):
        launcher.require_project('other-project')
    request = SimpleNamespace(goal='Исправить расчёт\nсуммы', raw_constraints=('Keep "x"', 'Preserve order', 'Keep "x"'))
    call = launcher._call(identity().ticket, request, SimpleNamespace(src=tmp_path), {})
    assert json.loads(call.prompt) == {'goal': request.goal, 'raw_constraints': list(request.raw_constraints)}


def report():
    return {'challenge': 'challenge', 'checks': {k: True for k in BOUNDARY_CHECKS},
            'environment': ['HOME', 'PATH'], 'network_namespace': 'net:[42]', 'interfaces': ['lo']}


@pytest.mark.parametrize('invalid', ['extra_env', 'missing_env', 'false_probe', 'missing_probe', 'stale',
                                     'shared_network', 'extra_interface'])
def test_probe_requires_full_environment_and_fresh_complete_evidence(invalid):
    body = report()
    q = SimpleNamespace(command_environment=('HOME', 'PATH'))
    assert len(verify_report(body, q, 'challenge', 'net:[12]')) == 64
    if invalid == 'extra_env':
        body['environment'].append('SSH_AUTH_SOCK')
    if invalid == 'missing_env':
        body['environment'].clear()
    if invalid == 'false_probe':
        body['checks']['deny_control'] = False
    if invalid == 'missing_probe':
        del body['checks']['deny_journal']
    if invalid == 'stale':
        body['challenge'] = 'old'
    if invalid == 'shared_network':
        body['network_namespace'] = 'net:[12]'
    if invalid == 'extra_interface':
        body['interfaces'].append('eth0')
    with pytest.raises(Refused):
        verify_report(body, q, 'challenge', 'net:[12]')


def test_runtime_has_one_writable_project_mount_and_explicit_command():
    spec = {'runtime': '/release', 'bwrap': '/bin/bwrap', 'workspace': '/state/active/src'}
    command = sandbox_command(spec, ['approved-client', 'arg'])
    assert command[command.index('--ro-bind')+1:][:2] == ['/release', '/']
    assert command[command.index('--bind')+1:][:2] == ['/state/active/src', '/work']
    assert command[command.index('--tmpfs') + 1:][:3] == ['/tmp', '--dir', '/tmp/home']
    assert command[command.index('TMPDIR') + 1] == '/tmp'
    assert command[command.index('HOME') + 1] == '/tmp/home'
    assert command[-3:] == ['--', 'approved-client', 'arg']
    assert '--unshare-all' in command and '--clearenv' in command


@pytest.mark.parametrize('mutation', [None, ('User', 'root'), ('ExecStart', '/bin/sh'),
                                     ('DropInPaths', '/tmp/override.conf'),
                                     ('FragmentPath', '/other/template'), ('Restart', 'always'),
                                     ('Delegate', 'no'), ('MemoryMax', 'infinity'),
                                     ('NeedDaemonReload', 'yes')])
def test_manager_verifies_effective_template_before_start(mutation):
    bound = identity()
    limits = ('1s', '30min', '4294967296', '256')
    q = SimpleNamespace(systemctl='/bin/systemctl', service_template='/etc/template', service_limits=limits,
                        controller_release='/release/entry', controller_python='/release/python', service_user='owner',
                        service_group='group', systemd_version='257')
    manager = SystemdManager(q, '/state')
    instance = bound.unit.split('@')[1].split('.')[0]
    start = '{ path=/release/python ; argv[]=/release/python -I -S /release/entry /state ' + instance
    start += ' ; ignore_errors=no ; pid=0 ; status=0/n/a }'
    fields = dict(Id=bound.unit, FragmentPath='/etc/template', DropInPaths='', NeedDaemonReload='no',
                  Restart='no', KillMode='control-group', Delegate='yes', User='owner', Group='group',
                  ExecStart=start, ExecStartPre='', ExecStartPost='', ExecCondition='', CPUQuotaPerSecUSec=limits[0],
                  RuntimeMaxUSec=limits[1], MemoryMax=limits[2], TasksMax=limits[3])
    if mutation:
        fields[mutation[0]] = mutation[1]
    manager._run = Mock(side_effect=['\n'.join(f'{key}={value}' for key, value in fields.items()), '257', ''])
    if mutation:
        with pytest.raises(Refused):
            manager.start(bound.unit)
        assert [c.args[0] for c in manager._run.call_args_list] == ['show']
    else:
        manager.start(bound.unit)
        assert manager._run.call_args.args == ('start', '--no-block', bound.unit)


def test_permission_checked_again_after_sandbox_probe(tmp_path, monkeypatch):
    binding = identity()
    observer = SimpleNamespace(directory=lambda _: tmp_path)
    qualification = SimpleNamespace(command_environment=('HOME', 'PATH'), verify=Mock(), fingerprint='q')
    launcher = SimpleNamespace(qualification=qualification)
    call = SimpleNamespace(prompt='task', probe_timeout=2, challenge='challenge', argv=('client',))
    receiver = LinuxReceiver(observer, binding, launcher, call=call)
    receiver.check_binding = Mock()
    monkeypatch.setattr(linux_native, 'wait_message', lambda *a: 0)
    (tmp_path/'probe-exit').write_text('0', encoding='ascii')
    monkeypatch.setattr(linux_native, 'message', lambda d, name: 0 if name == 'probe-exit' else report())
    monkeypatch.setattr(linux_native, 'owner_network_namespace', lambda: 'net:[12]')
    (tmp_path/'probe-output').write_text('probe', encoding='utf-8')
    published = []
    monkeypatch.setattr(linux_native, 'publish', lambda d, name, value: published.append(name))

    def authorize(action):
        if 'probe' in published:
            raise Refused('PERMIT_EXPIRED')
        return action()

    receiver.resume('task', authorize=authorize)
    assert published == ['probe']
    with pytest.raises(Refused, match='PERMIT_EXPIRED'):
        receiver.wait_result(['client'], 1)
    assert published == ['probe', 'probe-receipt']
    with pytest.raises(Refused):
        LinuxReceiver(observer, binding, launcher).resume('task', authorize=authorize)


@pytest.mark.parametrize('timed_out', [False, True])
def test_cancel_during_precheck_withholds_exec(tmp_path, monkeypatch, timed_out):
    binding = identity()
    observer = SimpleNamespace(directory=lambda _: tmp_path)
    launcher = SimpleNamespace(qualification=SimpleNamespace(verify=Mock()))
    call = SimpleNamespace(prompt='task', probe_timeout=120, argv=('client',))
    receiver = LinuxReceiver(observer, binding, launcher, call=call)
    receiver.check_binding = Mock()
    published = []
    monkeypatch.setattr(linux_native, 'publish', lambda d, name, value: published.append(name))
    receiver.resume('task', authorize=lambda action: action())
    if not timed_out:
        (tmp_path/'cancel').write_text('fixture', encoding='ascii')
    (tmp_path/'exit').write_text('125', encoding='ascii')
    outcome = receiver.wait_result(['client'], 1, timed_out=timed_out)
    assert outcome.returncode == 125 and outcome.timed_out == timed_out
    assert published == ['probe']
