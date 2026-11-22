"""Source-installed primary service contract with a disposable image and offline systemd readback."""
from configparser import ConfigParser
from dataclasses import replace
import hashlib
from pathlib import Path
from shutil import copyfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli.plugin_request_binding import canonical_home
from plugins.symphony_pm.transport import linux_sandbox as sandbox
from plugins.symphony_pm.transport.linux_host import SystemdManager
from plugins.symphony_pm.transport.types import Refused

TRANSPORT = Path(sandbox.__file__).parent
UNIT = '/etc/systemd/system/hermes-symphony@.service'
CONTROLLER = '/opt/hermes-symphony/controller'
ENTRY = CONTROLLER + '/hermes-agent/plugins/symphony_pm/transport/linux_entry.py'
STATE = '/var/lib/hermes-symphony/control'
INSTANCE = '7' * 64
NAME = 'hermes-symphony@' + INSTANCE + '.service'


@pytest.fixture
def installation(tmp_path, monkeypatch):
    def installed(name):
        return tmp_path / name.lstrip('/')

    python, runtime = CONTROLLER + '/bin/python3', '/opt/hermes-symphony/runtime'
    names = (UNIT, ENTRY, '/usr/bin/systemctl', '/usr/bin/bwrap', runtime + '/bin/client',
             runtime + '/bin/probe', runtime + '/etc/symphony/codex/config.toml')
    for name in (*names, python):
        path = installed(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode('ascii'))
    copyfile(TRANSPORT/'systemd'/Path(UNIT).name, installed(UNIT))
    copyfile(TRANSPORT/'linux_entry.py', installed(ENTRY))
    hashes = tuple(hashlib.sha256(installed(name).read_bytes()).hexdigest() for name in names)
    image = installed(runtime).stat()
    q = sandbox.LinuxQualification(
        profile_home=canonical_home(tmp_path), namespace='namespace', scheduler_id='scheduler', issuer_refs=('issuer',),
        installation_id='1'*64, host_id='2'*64, boot_id='3'*32, kernel='fixture', environment_ref='env',
        permission_profile='private', runtime=runtime, executable='/bin/client', probe_python='/bin/probe',
        service_template=UNIT, controller_release=ENTRY, controller_root=CONTROLLER, controller_python=python,
        controller_image_sha256='4'*64, service_user='hermes-symphony', service_group='hermes-symphony',
        systemd_version='257', systemctl=names[2], bwrap=names[3], hashes=hashes, evidence_sha256='5'*64,
        command_environment=('HOME',), settings_sha256='6'*64, marker_set_sha256='7'*64,
        image_sha256='8'*64, image_device=image.st_dev, image_inode=image.st_ino,
        service_limits=('1s', '30min', '4294967296', '256'))
    # Map only the qualified host tree into the fixture. File hashing and profile validation are real.
    def protected(path, *, directory=False):
        local = path if isinstance(path, Path) and tmp_path in path.parents else installed(str(path))
        assert local.is_dir() if directory else local.is_file()
        return local
    monkeypatch.setattr(sandbox, 'protected', protected)
    monkeypatch.setattr(sandbox, 'host_identity', lambda _: q.host_id)
    monkeypatch.setattr(sandbox, 'boot_identity', lambda: q.boot_id)
    monkeypatch.setattr(sandbox, 'summary', lambda: {'machine': {'os_release': q.kernel}})
    monkeypatch.setattr(sandbox, 'service_user_matches', lambda user, group: user == group == 'hermes-symphony')
    monkeypatch.setattr(sandbox, 'os', SimpleNamespace(ST_RDONLY=1, statvfs=lambda _: SimpleNamespace(f_flag=1)))
    monkeypatch.setattr(sandbox, 'verify_image', lambda *args: None)
    return q, installed


def readback(unit):
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(unit, encoding='utf-8')
    directives = dict(parser['Service'])
    argv = directives['ExecStart'].replace('%i', INSTANCE)
    fields = dict(directives, Id=NAME, FragmentPath=UNIT, DropInPaths='', NeedDaemonReload='no',
                  ExecStart='{ path=' + argv.split()[0] + ' ; argv[]=' + argv + ' ; ignore_errors=no ; pid=0 }')
    for hook in ('ExecStartPre', 'ExecStartPost', 'ExecCondition', 'ExecReload', 'ExecStop', 'ExecStopPost'):
        fields.setdefault(hook, '')
    cpu = int(directives['CPUQuota'].removesuffix('%')) / 100
    fields['CPUQuotaPerSecUSec'] = f'{cpu:g}s'
    fields['RuntimeMaxUSec'] = directives['RuntimeMaxSec']
    fields['TimeoutStopUSec'] = directives['TimeoutStopSec'] + 's'
    fields['MemoryMax'] = str(int(directives['MemoryMax'].removesuffix('G')) * 1024**3)
    return fields


def offline_manager(q, fields):
    manager = SystemdManager(q, STATE)
    def run(*args):
        if args[:2] == ('show', '--all'):
            props = args[2].removeprefix('--property=').split(',')
            return '\n'.join(f'{k}={fields[k]}' for k in props)
        return '257' if args[0] == 'show' else ''
    manager._run = Mock(side_effect=run)
    return manager


@pytest.mark.parametrize('mutation', [None, ('PrivateNetwork', 'no'), ('AmbientCapabilities', 'cap_net_admin'),
                                      ('Type', 'simple'), ('ExecStart', '/wrong/entry'),
                                      ('TasksMax', 'infinity')])
def test_packaged_primary_verifies_before_start(installation, mutation):
    q, installed = installation
    q.verify()
    fields = readback(installed(UNIT))
    if mutation:
        fields[mutation[0]] = mutation[1]
    manager = offline_manager(q, fields)
    if mutation:
        with pytest.raises(Refused, match='UNIT_CONFIGURATION'):
            manager.start(NAME)
        assert all(c.args[0] == 'show' for c in manager._run.call_args_list)
    else:
        manager.start(NAME)
        assert manager._run.call_args.args == ('start', '--no-block', NAME)


@pytest.mark.parametrize('name', [UNIT, ENTRY])
def test_primary_qualification_rejects_changed_installed_bytes(installation, name):
    q, installed = installation
    q.verify()
    with installed(name).open('ab') as asset:
        asset.write(b'\n')
    with pytest.raises(Refused, match='QUALIFIED_BYTES_CHANGED'):
        q.verify()


def test_custom_unit_at_same_path_retains_qualified_profile(installation):
    q, installed = installation
    installed(UNIT).write_bytes(installed(UNIT).read_bytes().replace(b'PrivateNetwork=yes', b'PrivateNetwork=no'))
    custom = replace(q, hashes=(hashlib.sha256(installed(UNIT).read_bytes()).hexdigest(), *q.hashes[1:]))
    custom.verify()
    manager = offline_manager(custom, readback(installed(UNIT)))
    manager.start(NAME)
    assert manager._run.call_args.args == ('start', '--no-block', NAME)


def test_doctor_reads_linux_qualification_without_opening_admission(installation, tmp_path):
    from dataclasses import replace
    from plugins.symphony_pm.doctor import doctor, record_qualification
    from plugins.symphony_pm.status_cli import journal_path
    q, installed = installation
    q = replace(q, runtime=str(installed(q.runtime)), namespace='symphony_pm:ingress-linux')
    path = journal_path(tmp_path, q.namespace)
    path.parent.mkdir(parents=True)
    owner = SimpleNamespace(journal=SimpleNamespace(path=path), scope=SimpleNamespace(namespace=q.namespace),
                            launcher=SimpleNamespace(qualification=q, settings=SimpleNamespace(attempts_root=tmp_path)))
    record_qualification(owner)
    report = doctor(tmp_path, q.namespace, path, [])
    assert report['qualification']['fingerprint'] == q.fingerprint
    assert report['qualification']['checks'] == {'config': True, 'executable': True}
    assert report['reasons'] == ['LINUX_LIVE_ADMISSION_UNOBSERVED'] and report['archive_count'] is None
    installed('/opt/hermes-symphony/runtime/bin/client').write_bytes(b'changed')
    assert 'EXECUTABLE_CHANGED_OR_MISSING' in doctor(tmp_path, q.namespace, path, [])['reasons']
