"""Optional systemd implementation of LinuxCheckHost. Import and construction have no effects."""
from dataclasses import asdict
import os
from pathlib import Path
import time

from .linux_check_config import CheckConfiguration, CheckManager, GitCheckConfiguration, GitCheckManager, MANIFEST, STATE, strict_json
from .linux_check_host import LinuxCheckHost, LinuxCheckInvocation, LinuxOwnerCheck
from .linux_check_wire import CheckGroup, verify_copy, read_request, terminal_from_proof, unit_name
from .linux_control import publish
from .linux_host import boot_identity, host_identity, private_directory, process_identity
from .types import Refused, digest, finite
from .linux_robot_check import GitCheckInvocation, LinuxGitCheckHost, LinuxGitChecks


class LinuxSystemdCheckHost(LinuxCheckHost):
    invocation_type = LinuxCheckInvocation

    def __init__(self, configuration):
        self.config = configuration

    def attested_profile(self):
        return self._attest(None)

    def _attest(self, deadline):
        if type(self.config).load() != self.config:
            raise Refused('CHECK_MANIFEST_CHANGED')
        self.config.verify()
        self._manager(deadline).verify_configuration(unit_name('0'*64, self.config.prefix))
        return self.config.invocation_profile

    def _manager(self, deadline):
        return CheckManager(self.config.installation, MANIFEST, deadline=deadline)

    def _check_intent(self, invocation):
        marker = strict_json(Path(self.config.installation.owner_state)/'owner-check.active')
        if marker['key'] != invocation.key or LinuxCheckInvocation.from_record(marker['invocation']) != invocation:
            raise Refused('CHECK_INTENT_CHANGED')

    def _directory(self, key):
        return private_directory(STATE)/unit_name(key)

    def _root_member(self, bound):
        path = Path('/sys/fs/cgroup')/bound.cgroup.lstrip('/')
        info = path.stat()
        if ((info.st_dev, info.st_ino) != (bound.device, bound.inode) or process_identity(bound.root.pid) != bound.root
                or '0::'+bound.cgroup not in os.fsdecode(Path(f'/proc/{bound.root.pid}/cgroup').read_bytes()).splitlines()):
            raise Refused('CHECK_ROOT_MEMBERSHIP')

    def _authorize(self, invocation, directory, manager, deadline):
        while time.monotonic() < deadline:
            try:
                raw = strict_json(directory/'ready')
            except FileNotFoundError:
                time.sleep(min(0.04, max(0.0, deadline - time.monotonic())))
                continue
            bound = CheckGroup.parse(raw, invocation, self.config)
            if bound.request_sha256 != digest(strict_json(directory/'request')):
                raise Refused('CHECK_SERVICE_REQUEST')
            if self._attest(deadline) != invocation.profile:
                raise Refused('CHECK_QUALIFICATION_CHANGED')
            manager.verify_configuration(bound.unit)
            meta = manager.describe(bound.unit)
            if (meta['InvocationID'], meta['ControlGroup']+'/payload') != (bound.service_id, bound.cgroup):
                raise Refused('CHECK_SERVICE_REPLACED')
            self._root_member(bound)
            verify_copy(self.config, invocation)
            if time.monotonic() >= deadline:
                break
            publish(directory, 'grant', {'key': invocation.key, 'binding': digest(asdict(bound))})
            return
        raise TimeoutError('CHECK_START_TIMEOUT')

    def start(self, invocation, *, timeout):
        if type(invocation) is not self.invocation_type or not finite(timeout) or timeout != self.config.profile.timeout:
            raise Refused('CHECK_SERVICE_REQUEST')
        started = time.monotonic()
        deadline = started + timeout
        invocation.validate()
        if (self._attest(deadline) != invocation.profile or invocation.command not in self.config.profile.commands):
            raise Refused('CHECK_SERVICE_REQUEST')
        self._check_intent(invocation)
        verify_copy(self.config, invocation)
        directory = self._directory(invocation.key)
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise Refused('CHECK_SERVICE_START_SPENT') from exc
        from .linux_control import _sync_directory
        _sync_directory(directory.parent)
        publish(directory, 'request', {'schema': 1, 'key': invocation.key, 'invocation': asdict(invocation),
                                        'started': started, 'expires': deadline})
        manager = self._manager(deadline)
        manager.start(unit_name(invocation.key, self.config.prefix))
        self._authorize(invocation, directory, manager, deadline)

    def observe(self, invocation, *, timeout):
        if type(invocation) is not self.invocation_type or not finite(timeout) or not 0 <= timeout <= self.config.profile.timeout:
            raise Refused('CHECK_SERVICE_OBSERVATION')
        deadline = time.monotonic() + timeout
        config = self.config.installation
        if (boot_identity(), host_identity(config.installation_id)) != (config.boot_id, config.host_id):
            raise Refused('CHECK_SERVICE_HOST_CHANGED')
        directory = self._directory(invocation.key)
        target, request = read_request(directory, invocation.key, self.config)
        if target != invocation:
            raise Refused('CHECK_SERVICE_REQUEST')
        while True:
            try:
                stop = strict_json(directory/'stop')
            except FileNotFoundError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                time.sleep(min(remaining, 0.04))
                continue
            bound = CheckGroup.parse(strict_json(directory/'ready'), invocation, self.config)
            try:
                grant = strict_json(directory/'grant')
            except FileNotFoundError:
                grant = None
            if type(stop) is not dict:
                raise Refused('CHECK_GROUP_STOP_UNPROVEN')
            expected = {'key': invocation.key, 'binding': digest(asdict(bound))}
            # A matching grant can arrive while the controller is checking the deadline. Only
            # its sealed pipe readback establishes that execution was withheld in that cut.
            if (grant is not None and grant != expected
                    or stop.get('release') == 'started' and grant != expected):
                raise Refused('CHECK_SERVICE_GRANT')
            if not finite(stop.get('observed')) or stop['observed'] > time.monotonic():
                raise Refused('CHECK_GROUP_STOP_UNPROVEN')
            return terminal_from_proof(stop, invocation, bound, request)


class LinuxSystemdGitCheckHost(LinuxSystemdCheckHost, LinuxGitCheckHost):
    invocation_type = GitCheckInvocation

    @property
    def export_root(self):
        return Path(self.config.profile.root)

    def _manager(self, deadline):
        return GitCheckManager(self.config.installation, self.config.manifest, deadline=deadline)

    def _directory(self, key):
        return private_directory(self.config.state)/unit_name(key, self.config.prefix)

    def _check_intent(self, invocation):
        marker = Path(self.config.installation.owner_state)/'robot-git-check.active'/'invocation.json'
        if GitCheckInvocation.from_record(strict_json(marker)) != invocation:
            raise Refused('CHECK_INTENT_CHANGED')


def systemd_git_check(qualification, primary, settings):
    from .check_command import DIFF_CHECK
    config = GitCheckConfiguration.load()
    profile = config.profile
    system = config.installation
    if (system.installation_id, system.host_id, system.boot_id) != (
            qualification.installation_id, qualification.host_id, qualification.boot_id):
        raise Refused('CHECK_SERVICE_HOST_CHANGED')
    expected = tuple(c for c in primary.profile.commands if c.requires_git and c != DIFF_CHECK)
    if (not expected or profile.commands != expected or profile.launch_fingerprint != primary.profile.fingerprint
            or (profile.git, profile.git_sha256, profile.timeout) !=
               (primary.profile.git, primary.profile.git_sha256, primary.profile.timeout)
            or config.installation.owner_state != settings.state):
        raise Refused('LINUX_GIT_CHECK_QUALIFICATION_CHANGED')
    return LinuxGitChecks(profile.evidence_sha256, LinuxSystemdGitCheckHost(config))


def systemd_owner_check(qualification, *, manifest=MANIFEST):
    """Explicit opt-in by the profile owner. A missing installation cannot activate this checker."""
    config = CheckConfiguration.load(manifest)
    if config.profile.launch_fingerprint != qualification.fingerprint:
        raise Refused('CHECK_QUALIFICATION_CHANGED')
    host = LinuxSystemdCheckHost(config)
    return LinuxOwnerCheck(config.profile, host)
