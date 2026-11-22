"""Offline Git-check service integration. The unit manager and cgroup observations are synthetic."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from plugins.symphony_pm.status_cli import read_launches
from plugins.symphony_pm.transport import linux_check_config, linux_check_wire, linux_control, linux_sandbox
from plugins.symphony_pm.transport import linux_systemd_check as service
from plugins.symphony_pm.transport.check_command import DIFF_CHECK
from plugins.symphony_pm.transport.linux_bootstrap import linux_exec_transport
from plugins.symphony_pm.transport.linux_check_config import CheckConfiguration, GitCheckConfiguration, GitCheckInstallation
from plugins.symphony_pm.transport.linux_check_wire import CheckGroup, read_request
from plugins.symphony_pm.transport.types import ProcessIdentity, Refused, digest
from tests.plugins.test_symphony_pm_codex_exec import _git
from tests.plugins.test_symphony_pm_linux_owner_check import checker_stand
from tests.plugins.test_symphony_pm_linux_primary_unit import installation
from tests.plugins.test_symphony_pm_linux_typed_apply import FixtureGitHost, linux_chat
from tests.plugins.test_symphony_pm_typed_approval import displayed


def test_separate_git_manifest_and_evidence_bind_the_installed_profile(linux_chat, installation, tmp_path, monkeypatch):
    stand, primary, *_ = linux_chat
    q, _ = installation
    system = GitCheckInstallation(q.installation_id, q.host_id, q.boot_id, q.kernel, q.systemd_version,
        stand.launcher.settings.state, q.systemctl, q.bwrap, q.controller_image_sha256, '1'*64,
        '2'*64, '3'*64, '4'*64, ('1s', '2min', '536870912', '128'), 1<<23)
    commands = tuple(c for c in primary.commands if c.requires_git and c != DIFF_CHECK)
    profile = replace(primary, launch_fingerprint=primary.fingerprint, commands=commands)
    path = tmp_path/'git-check.json'
    def protected(name):
        assert name == GitCheckConfiguration.manifest
        return path
    monkeypatch.setattr(linux_check_config, 'protected', protected)
    with pytest.raises(FileNotFoundError):
        service.systemd_git_check(q, stand.launcher.owner_check, stand.launcher.settings)
    path.write_text(json.dumps(dict(schema=1, installation=asdict(system), profile=asdict(profile))), encoding='ascii')
    config = GitCheckConfiguration.load()
    checker = service.systemd_git_check(q, stand.launcher.owner_check, stand.launcher.settings)
    assert checker.host.config == config
    evidence = dict(schema=1, host=q.host_id, boot=q.boot_id, policy=config.policy_digest,
                    receipts=dict.fromkeys(config.checks, 'a'*64))
    config.verify_evidence(evidence)
    with pytest.raises(Refused, match='CHECK_VM_EVIDENCE'):
        config.verify_evidence(dict(evidence, receipts=dict.fromkeys(CheckConfiguration.checks, 'a'*64)))
    for key in ('host_id', 'boot_id', 'runtime_image_sha256', 'controller_image_sha256', 'template_sha256',
                'systemctl_sha256', 'bwrap_sha256'):
        changed = replace(config, installation=replace(system, **{key: 'e'*len(getattr(system, key))}))
        with pytest.raises(Refused, match='CHECK_VM_EVIDENCE'):
            changed.verify_evidence(evidence)
    for values in (dict(commands=tuple()), dict(git_sha256='e'*64), dict(launch_fingerprint='e'*64)):
        with pytest.raises(Refused, match='CHECK_VM_EVIDENCE'):
            replace(config, profile=replace(profile, **values)).verify_evidence(evidence)
    with pytest.raises(Refused, match='CHECK_MANIFEST_PATH'):
        GitCheckConfiguration.load(CheckConfiguration.manifest)
    with pytest.raises(Refused, match='CHECK_SERVICE_HOST_CHANGED'):
        service.systemd_git_check(replace(q, boot_id='e'*32), stand.launcher.owner_check, stand.launcher.settings)


class SyntheticManager:
    """Publish only synthetic systemd/cgroup observations; Git and the test process are real."""

    def __init__(self, config):
        self.config = config
        self.unit = None
        self.starts = 0
        self.proof = None
        self.worker = FixtureGitHost(config.invocation_profile)

    def start(self, unit):
        self.starts += 1
        self.unit = unit
        directory = Path(self.config.state)/unit
        key = unit.removeprefix(self.config.prefix).removesuffix('.service')
        inv, request = read_request(directory, key, self.config)
        argv = linux_check_wire.sandbox_argv(self.config, inv)
        assert argv[argv.index('--ro-bind') + 1] == self.config.runtime
        assert [inv.directory, '/work'] == argv[argv.index(inv.directory):argv.index(inv.directory)+2]
        assert '--unshare-all' in argv and '--clearenv' in argv
        assert argv[argv.index('GIT_CONFIG_GLOBAL') + 1] == '/dev/null'
        assert argv[argv.index('GIT_CONFIG_NOSYSTEM') + 1] == '1'
        system = self.config.installation
        self.bound = CheckGroup(key, inv.profile, inv.epoch, system.host_id, system.boot_id, unit, 'a'*32,
                                digest(request), '/system.slice/'+unit+'/payload', 42, 73, ProcessIdentity(101, '883'))
        linux_control.publish(directory, 'ready', asdict(self.bound))

    def verify_configuration(self, unit):
        assert unit.startswith(self.config.prefix)

    def describe(self, unit):
        assert unit == self.unit
        return {'InvocationID': self.bound.service_id, 'ControlGroup': '/system.slice/'+unit}

    def publish(self, directory, name, value):
        linux_control.publish(directory, name, value)
        if name == 'grant':
            inv, _ = read_request(directory, self.bound.key, self.config)
            self.worker.start(inv, timeout=30)
            result = self.worker.observe(inv, timeout=30)
            self.proof = dict(schema=1, binding=digest(asdict(self.bound)), release='started', sealed=True,
                              populated=0, processes=[dict(pid=101, creation='883', gone=True)],
                              observed=time.monotonic(), returncode=result.returncode, timed_out=False)
            if self.starts > 1:
                linux_control.publish(directory, 'stop', self.proof)


@pytest.fixture
def installed_host(linux_chat, installation, tmp_path_factory, monkeypatch, request):
    stand, primary, _, _, _, _, _ = linux_chat
    work = tmp_path_factory.mktemp('git')
    spool, scratch = work/'spool', work/'scratch'
    spool.mkdir()
    scratch.mkdir()
    q = replace(installation[0], namespace='symphony_pm:ingress-linux')
    monkeypatch.setattr(linux_sandbox, 'protected', lambda path: Path(path))
    launcher = stand.launcher
    if request.param:
        followup = replace(primary.commands[-1], name='followup-git')
        primary = replace(primary, commands=(*primary.commands, followup))
        launcher.owner_check = replace(launcher.owner_check, profile=primary)
        launcher.owner_check.host.profile = primary
    system = SimpleNamespace(host_id=q.host_id, boot_id=q.boot_id, installation_id=q.installation_id,
                             owner_state=launcher.settings.state, bwrap='/fixture/bwrap')
    profile = replace(primary, root=str(scratch), launch_fingerprint=primary.fingerprint, evidence_sha256='a'*64,
                      commands=tuple(c for c in primary.commands if c.requires_git and c != DIFF_CHECK))
    cfg = GitCheckConfiguration(system, profile)
    manager = SyntheticManager(cfg)
    monkeypatch.setattr(GitCheckConfiguration, 'state', str(spool))
    monkeypatch.setattr(GitCheckConfiguration, 'load', classmethod(lambda cls, *args: cfg))
    monkeypatch.setattr(GitCheckConfiguration, 'verify', lambda self: None)
    monkeypatch.setattr(service, 'GitCheckManager', lambda *args, **kwargs: manager)
    monkeypatch.setattr(service, 'private_directory', lambda path: Path(path))
    monkeypatch.setattr(service, 'boot_identity', lambda: system.boot_id)
    monkeypatch.setattr(service, 'host_identity', lambda _: system.host_id)
    monkeypatch.setattr(service, 'publish', manager.publish)
    monkeypatch.setattr(linux_check_wire, 'os', SimpleNamespace(readlink=lambda _: 'fixture-namespace'))
    monkeypatch.setattr(service.LinuxSystemdGitCheckHost, '_root_member', lambda self, bound: None)
    bootstrap = linux_exec_transport(profile_home=q.profile_home, namespace=q.namespace, qualification=q,
        settings=launcher.settings, approval_transport=object(), scheduler_id=q.scheduler_id, controller='fixture',
        trusted_issuers=q.issuer_refs, owner_check=launcher.owner_check, systemd_git_checks=True)
    launcher.git_checks = bootstrap.launcher.git_checks
    assert type(launcher.git_checks.host) is service.LinuxSystemdGitCheckHost
    return manager


@pytest.mark.parametrize('installed_host', [False, True], indirect=True, ids=['last-check', 'remaining-check'])
def test_late_git_terminal_is_query_only_and_survives_interrupted_save(linux_chat, installed_host, monkeypatch,
                                                                        record_property):
    stand, _, _, finished, failures, session, _ = linux_chat
    manager = installed_host
    # For a missing stop record exercise the real nonblocking query without waiting for the fixture deadline.
    host = stand.launcher.git_checks.host
    observe = host.observe
    monkeypatch.setattr(host, 'observe', lambda invocation, *, timeout: observe(invocation, timeout=0))
    marker = Path(stand.launcher.settings.state)/'robot-git-check.active'
    with session() as channel:
        _, approve = displayed(channel)
        assert channel.deliver(approve).startswith('APPROVED.')
        assert channel._submit('approve') == 'SUBMITTED'
        assert finished.wait(30) and not failures
        row, = read_launches(channel.owner.journal.path)
        job, patch = row['job'], row['result']['patch']['sha256']
        command = f'/symphony apply {job} {patch}'
        ref = 'refs/heads/symphony/'+job
        ref_writes = []
        run = subprocess.run

        def count_publications(argv, *args, **kwargs):
            if 'update-ref' in argv and ref in argv:
                ref_writes.append(ref)
            return run(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, 'run', count_publications)
        assert channel.deliver(command, message_id='7001') == 'ROBOT_CHECK_INCOMPLETE'
        assert manager.starts == 1 and marker.is_dir()
    with session(restart=True) as recovered:
        owner = recovered.owner
        stop = Path(manager.config.state)/manager.unit/'stop'
        assert marker.is_dir() and not stop.exists()
        stop.write_text(json.dumps(dict(manager.proof, binding='0'*64)), encoding='ascii')
        owner.reconcile()
        assert marker.exists()
        stop.write_text(json.dumps(manager.proof), encoding='ascii')
        with monkeypatch.context() as cut:
            cut.setattr(service, 'boot_identity', lambda: '0'*32)
            owner.reconcile()
            assert marker.exists()
        with monkeypatch.context() as cut:
            def interrupt(*args, **kwargs):
                raise OSError('fixture interrupted terminal commit')
            cut.setattr(owner.journal, '_append', interrupt)
            owner.reconcile()
            assert marker.exists()
    with session(restart=True) as recovered:
        assert not marker.exists()
        inv, = manager.worker.calls
        assert manager.starts == 1 and not Path(inv.directory).exists()
        recovered.owner.reconcile()
        saved, = read_launches(recovered.owner.journal.path)
        receipts = [r['body'] for r in saved['observations'] if r['body'].get('state') == 'CHECK_RECOVERED']
        receipt, = receipts
        assert receipt['terminal']['invocation'] == inv.key and receipt['terminal']['returncode'] == 0
        assert not _git('for-each-ref', '--format=%(refname)', ref, cwd=stand.repo)
        assert recovered.deliver(command, message_id='7001') == 'ROBOT_NEW_APPLY_COMMAND_REQUIRED'
        rotated = len(manager.config.profile.commands) > 1
        if rotated:
            recovered.entry.session_id = 'current-owner-after-rotation'
            assert recovered.deliver(command, message_id='7001') == 'ROBOT_NEW_APPLY_COMMAND_REQUIRED'
            stranger = replace(recovered.source, user_id='other-owner')
            other_chat = replace(recovered.source, chat_id='other-chat')
            assert recovered.deliver(command, message_id='7002', source=stranger) == 'OWNER_SENDER_REQUIRED'
            assert recovered.deliver(command, message_id='7003', source=other_chat) == 'OWNER_SENDER_REQUIRED'
        assert manager.starts == 1 and not ref_writes
        assert not _git('for-each-ref', '--format=%(refname)', ref, cwd=stand.repo)
        fresh = '7004' if rotated else '7002'
        final = json.loads(recovered.deliver(command, message_id=fresh))
        assert final['state'] == 'APPLIED' and final['commit'] == inv.commit
        assert final['command_message']['session_id'] == recovered.entry.session_id
        expected_starts = len(manager.config.profile.commands)
        assert len(final['git_checks']) == expected_starts + 1 and manager.starts == expected_starts
        assert json.loads(recovered.deliver(command, message_id=fresh)) == final
        assert json.loads(recovered.deliver(command, message_id='7001')) == final
        assert manager.starts == expected_starts and ref_writes == [ref]
        record_property('linux_offline', dict(unit_manager='synthetic', cgroup='synthetic', check='pytest',
                        starts=manager.starts, replay_starts=0, reads_after_restart=True, late_receipts=len(receipts),
                        commit=inv.commit, terminal=receipt['terminal'], automatic_ref_writes=0,
                        spent_message='7001', fresh_message=fresh, session_rotated=rotated,
                        total_ref_writes=len(ref_writes)))
