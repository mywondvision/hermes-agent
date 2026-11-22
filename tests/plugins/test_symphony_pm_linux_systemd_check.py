"""Real check-host dispatch, serialization and recovery with an inert manager and kernel readback."""
import builtins
from dataclasses import asdict, replace
from io import BytesIO, StringIO
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from plugins.symphony_pm.transport import linux_check_service as service
from plugins.symphony_pm.transport import linux_systemd_check as native
from plugins.symphony_pm.transport import linux_check_wire, linux_control, linux_probe
from plugins.symphony_pm.transport.check_command import CheckCommand
from plugins.symphony_pm.transport.linux_check_config import CheckConfiguration, checked_arguments, strict_json
from plugins.symphony_pm.transport.linux_check_host import LinuxCheckInvocation, LinuxCheckProfile
from plugins.symphony_pm.transport.linux_check_wire import CheckGroup, read_request, unit_name
from plugins.symphony_pm.transport.recapture import directory_identity
from plugins.symphony_pm.transport.types import ProcessIdentity, Refused, digest


@pytest.fixture
def stand(tmp_path, monkeypatch):
    owner, state, scratch = (tmp_path/name for name in ('owner', 'control', 'scratch'))
    for d in (owner, state, scratch):
        d.mkdir()
    src = scratch/'c-input'/'src'
    src.mkdir(parents=True)
    command = CheckCommand('pytest', 'python', ('-I','-B','-m','pytest','tests/test_value.py'))
    profile = LinuxCheckProfile('a'*64, 'b'*64, str(scratch), str(tmp_path/'git'), 'c'*64, (command,), 30)
    install = SimpleNamespace(installation_id='0'*64, host_id='1'*64, boot_id='2'*32, owner_state=str(owner),
                              bwrap='/qualified/bwrap')
    config = CheckConfiguration(install, profile)
    ticket = LinuxCheckInvocation('0'*32, profile.fingerprint, 'ns', 'job', '1'*64, 'attempt-one',
        '2'*64, 3, '3'*40, '4'*64, '5'*40, str(src), tuple(directory_identity(src)),
        tuple(directory_identity(src.parent)), command)
    unit = unit_name(ticket.key)
    directory = state/unit
    manager = SimpleNamespace(starts=0, invocation='6'*32, configured=True)
    def start(name):
        assert name == unit
        manager.starts += 1
        inv, req = read_request(directory, ticket.key, config)
        binding = CheckGroup(inv.key, inv.profile, inv.epoch, install.host_id, install.boot_id, name,
            manager.invocation, digest(req), '/system.slice/'+name+'/payload', 42, 71, ProcessIdentity(101,'8812'))
        linux_control.publish(directory, 'ready', asdict(binding))
    manager.start = start
    def verify(unit):
        if not manager.configured:
            raise Refused('CHECK_SERVICE_BOUNDARY')
    manager.verify_configuration = verify
    manager.describe = lambda name: {'InvocationID': manager.invocation, 'ControlGroup': '/system.slice/'+name}
    monkeypatch.setattr(native, 'STATE', str(state))
    monkeypatch.setattr(native, 'private_directory', lambda p: Path(p))
    monkeypatch.setattr(native, 'CheckManager', lambda *a, **k: manager)
    monkeypatch.setattr(CheckConfiguration, 'load', classmethod(lambda cls, *a: config))
    monkeypatch.setattr(CheckConfiguration, 'verify', lambda self: None)
    monkeypatch.setattr(native, 'boot_identity', lambda: install.boot_id)
    monkeypatch.setattr(native, 'host_identity', lambda _: install.host_id)
    monkeypatch.setattr(native.LinuxSystemdCheckHost, '_root_member', lambda self, bound: None)
    monkeypatch.setattr(linux_control, '_sync_directory', lambda _: None)
    linux_control.publish(owner, 'owner-check.active', {'key': ticket.key, 'invocation': asdict(ticket)})
    checker = native.systemd_owner_check(SimpleNamespace(fingerprint=profile.launch_fingerprint))
    return SimpleNamespace(config=config, checker=checker, host=checker.host, owner=owner, state=state,
                           manager=manager, ticket=ticket, directory=directory, src=src)


def synthetic_readback(r, monkeypatch, *, release='started'):
    inv, req = read_request(r.directory, r.ticket.key, r.config)
    bound = CheckGroup.parse(strict_json(r.directory/'ready'), inv, r.config)
    # No service, process or cgroup exists on the fixture host.
    import os
    class Kernel:
        @staticmethod
        def waitpid(pid, flags):
            assert pid == bound.root.pid
            return pid, 125 << 8 if release == 'never_granted' else 0
        WNOHANG = 1
        waitstatus_to_exitcode = staticmethod(os.waitstatus_to_exitcode)
    monkeypatch.setattr(service, 'os', Kernel)
    monkeypatch.setattr(service, '_group_identity', lambda fd, group, exact: None)
    monkeypatch.setattr(service, 'members', lambda fd, path: ())
    monkeypatch.setattr(service, 'group_values', lambda fd: 0)
    monkeypatch.setattr(service, 'process_identity', lambda pid: None)
    if release == 'never_granted':
        # Use the captured monotonic times without sleeping for the fixture deadline.
        monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: req['expires'] + 1))
        monkeypatch.setattr(native, 'time', SimpleNamespace(monotonic=lambda: req['expires'] + 2))
    service.supervise(r.directory, r.directory/'unused', 47, bound, req, release)


def test_dispatch_restart_observes_exact_durable_empty_group_once(stand, monkeypatch):
    r = stand
    r.host.start(r.ticket, timeout=30)
    with pytest.raises(Refused, match='START_SPENT'):
        r.host.start(r.ticket, timeout=30)
    assert r.manager.starts == 1 and r.host.observe(r.ticket, timeout=0) is None
    synthetic_readback(r, monkeypatch)
    successor = native.LinuxSystemdCheckHost(r.config)
    terminal = successor.observe(r.ticket, timeout=0)
    assert terminal.matches(r.ticket) and terminal.returncode == 0 and not terminal.timed_out
    assert terminal.stop_receipt_sha256 == digest(strict_json(r.directory/'stop'))
    assert r.manager.starts == 1


@pytest.mark.parametrize('matching_grant', [False, True])
def test_pipe_never_opened_only_proves_negative_outcome(stand, monkeypatch, matching_grant):
    r = stand
    def cut(*args):
        raise TimeoutError('fixture cut before grant')
    if matching_grant:
        r.host.start(r.ticket, timeout=30)
    else:
        monkeypatch.setattr(r.host, '_authorize', cut)
        with pytest.raises(TimeoutError):
            r.host.start(r.ticket, timeout=30)
    assert (r.directory/'grant').exists() == matching_grant
    synthetic_readback(r, monkeypatch, release='never_granted')
    terminal = native.LinuxSystemdCheckHost(r.config).observe(r.ticket, timeout=0)
    assert terminal.matches(r.ticket) and terminal.returncode == 125 and terminal.timed_out
    assert r.manager.starts == 1


@pytest.mark.parametrize(('field', 'value'), [('populated', 1), ('sealed', False), ('binding', '0'*64),
                                             ('processes', []), ('release', 'unknown')])
def test_corrupt_stop_receipts_keep_hold(stand, monkeypatch, field, value):
    r = stand
    r.host.start(r.ticket, timeout=30)
    synthetic_readback(r, monkeypatch)
    stop = r.directory/'stop'
    original = stop.read_bytes()
    for bad, error in ((b'\xef\xbb\xbf' + original, ValueError),
                       (b'{"schema":1,' + original.lstrip()[1:], Refused)):
        stop.write_bytes(bad)
        with pytest.raises(error):
            r.host.observe(r.ticket, timeout=0)
    stop.write_bytes(original)
    proof = strict_json(stop)
    proof[field] = value
    (r.directory/'stop').write_text(json.dumps(proof), encoding='utf-8')
    with pytest.raises(Refused):
        r.host.observe(r.ticket, timeout=0)
    assert r.manager.starts == 1


def test_future_observation_cannot_settle(stand, monkeypatch):
    r = stand
    r.host.start(r.ticket, timeout=30)
    synthetic_readback(r, monkeypatch)
    proof = strict_json(r.directory/'stop')
    proof['observed'] = time.monotonic() + 15
    (r.directory/'stop').write_text(json.dumps(proof), encoding='utf-8')
    with pytest.raises(Refused, match='STOP_UNPROVEN'):
        r.host.observe(r.ticket, timeout=0)


def test_qualification_changed_after_ready_withholds_grant(stand):
    r = stand
    initial = r.manager.start
    def changed(unit):
        initial(unit)
        r.manager.configured = False
    r.manager.start = changed
    with pytest.raises(Refused, match='BOUNDARY'):
        r.host.start(r.ticket, timeout=30)
    assert (r.directory/'ready').is_file() and not (r.directory/'grant').exists()


def test_descendant_until_after_deadline_forces_timeout(stand, monkeypatch):
    r = stand
    r.host.start(r.ticket, timeout=30)
    inv, req = read_request(r.directory, r.ticket.key, r.config)
    bound = CheckGroup.parse(strict_json(r.directory/'ready'), inv, r.config)
    import os
    kernel = SimpleNamespace(waitpid=lambda *args: (bound.root.pid, 0), WNOHANG=1,
                             waitstatus_to_exitcode=os.waitstatus_to_exitcode)
    monkeypatch.setattr(service, 'os', kernel)
    monkeypatch.setattr(service, 'process_identity', lambda _: None)
    monkeypatch.setattr(service, 'members', lambda *args: ())
    monkeypatch.setattr(service, '_group_identity', lambda *args: None)
    monkeypatch.setattr(service, '_term_bound', lambda *args: None)
    counts = iter([1, 0, 0])
    monkeypatch.setattr(service, 'group_values', lambda _: next(counts))
    times = iter([req['expires'] - 0.01, req['expires'] + 0.01])
    monkeypatch.setattr(service, 'time', SimpleNamespace(monotonic=lambda: next(times), sleep=lambda _: None))
    monkeypatch.setattr(native, 'time', SimpleNamespace(monotonic=lambda: req['expires'] + 2))
    service.supervise(r.directory, r.directory/'unused', 47, bound, req, 'started')
    terminal = r.host.observe(inv, timeout=0)
    assert terminal.timed_out and terminal.returncode == 0


def test_before_manager_start_changed_config_or_unapproved_command_refuses(stand):
    r = stand
    r.manager.configured = False
    with pytest.raises(Refused, match='BOUNDARY'):
        r.host.start(r.ticket, timeout=30)
    r.manager.configured = True
    changed = replace(r.ticket, command=CheckCommand('unapproved', 'python', ('-c', 'x')))
    with pytest.raises(Refused, match='REQUEST'):
        r.host.start(changed, timeout=30)
    assert r.manager.starts == 0


def test_only_allowlisted_pytest_and_ruff_target_forms(stand, monkeypatch):
    monkeypatch.setattr(linux_check_wire, 'os', SimpleNamespace(readlink=lambda path: 'fixture'))
    plan = linux_check_wire.sandbox_argv(stand.config, stand.ticket)
    assert plan[plan.index('--tmpfs') + 1:][:3] == ['/tmp', '--dir', '/tmp/home']
    assert plan[plan.index('TMPDIR') + 1] == '/tmp'
    assert plan[plan.index('HOME') + 1] == '/tmp/home'
    assert '--basetemp=/tmp/pytest' in plan
    assert_guest_probe_writes(plan[plan.index('-c') + 1], ['{}', '[]'], '/tmp/check-scratch')
    assert_guest_probe_writes(linux_probe.PROGRAM, ['nonce', '{"hidden":{}}'], '/tmp/.symphony-probe-nonce')
    cmd = CheckCommand('lint', 'ruff', ('check','src'))
    assert checked_arguments(cmd) == ['/usr/bin/ruff','check','--no-cache','--isolated','--','src']
    assert checked_arguments(CheckCommand('test','python',('-I','-B','-m','pytest','tests/test_unit.py')))[-1] == 'tests/test_unit.py'
    for bad in (replace(cmd, arguments=('check','../owner')), replace(cmd, arguments=('check','--config=x')),
                replace(cmd, tool='git'), replace(cmd, tool='python', arguments=('-c','x'))):
        with pytest.raises(Refused):
            checked_arguments(bad)


def assert_guest_probe_writes(program, arguments, expected):
    # Run the installed probe up to its scratch write against isolated guest I/O.
    class ScratchOpened(Exception):
        pass

    def guest_open(path, mode='r', **kwargs):
        if path == '/proc/net/dev':
            return StringIO('header\nheader\nlo: 0\n')
        assert mode == 'xb'
        if path.startswith('/work/'):
            return BytesIO()
        raise ScratchOpened(path)

    guest_os = SimpleNamespace(environ=dict.fromkeys(
        ('HOME', 'TMPDIR', 'PATH', 'LANG', 'LC_ALL', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD')),
        path=SimpleNamespace(lexists=lambda path: False),
        statvfs=lambda path: SimpleNamespace(f_flag=1), ST_RDONLY=1, unlink=lambda path: None)
    modules = {'json': json, 'os': guest_os, 'socket': SimpleNamespace(),
               'sys': SimpleNamespace(argv=['probe', *arguments])}
    guest_builtins = {**vars(builtins), '__import__': lambda name, *args: modules[name]}
    with pytest.raises(ScratchOpened) as opened:
        exec(program, {'__builtins__': guest_builtins, 'open': guest_open})
    assert opened.value.args == (expected,)


def test_replaced_path_between_open_and_kill_withholds_effect(monkeypatch):
    effects = []
    class Kernel:
        O_WRONLY, O_NOFOLLOW, O_CLOEXEC = 1, 2, 4
        def open(self, name, flags, *, dir_fd):
            assert name == 'cgroup.kill' and dir_fd == 17 and flags == 7
            return 18
        def write(self, fd, data):
            effects.append((fd, data))
        def close(self, fd):
            assert fd == 18
    monkeypatch.setattr(service, 'os', Kernel())
    def replaced(*args):
        raise Refused('CHECK_CGROUP_CHANGED')
    monkeypatch.setattr(service, '_group_identity', replaced)
    with pytest.raises(Refused, match='CGROUP_CHANGED'):
        service._kill_bound(17, Path('replaced'), None)
    assert not effects
