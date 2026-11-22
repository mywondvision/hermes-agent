"""Root-provisioned qualification for the optional Linux checker service."""
from dataclasses import asdict, dataclass
import time
import os
from pathlib import Path, PurePosixPath
import re

from hermes_platform.host.summary import summary
from .check_command import CheckCommand
from .linux_check_host import LinuxCheckProfile
from .linux_control import strict_json
from .linux_host import boot_identity, host_identity, private_directory, service_user_matches, SystemdManager
from .linux_sandbox import GUEST_SCRATCH, file_digest, in_image, protected, verify_image
from .types import Refused, digest, sha

MANIFEST = '/etc/hermes-symphony/owner-check.json'
EVIDENCE = '/etc/hermes-symphony/owner-check-evidence.json'
TEMPLATE = '/etc/systemd/system/hermes-symphony-check@.service'
CONTROLLER = '/opt/hermes-symphony/controller'
ENTRY = CONTROLLER + '/hermes-agent/plugins/symphony_pm/transport/linux_check_entry.py'
PYTHON = CONTROLLER + '/bin/python3'
RUNTIME = '/opt/hermes-symphony/check-runtime'
STATE = '/var/lib/hermes-symphony/check-control'
SCRATCH = '/var/lib/hermes-symphony/check-scratch'
CHECKS = ('readonly_tree', 'isolated_tmp', 'no_git', 'no_network', 'no_owner_files', 'no_control_sockets',
          'no_inherited_credentials', 'cgroup_drain', 'restart_readback')
PREFIX = 'hermes-symphony-check@'


def checked_arguments(command):
    """These two forms select a trusted tool and relative input paths. No arbitrary CLI options."""
    if command.tool == 'ruff':
        prefix = ('check',)
        argv = ['/usr/bin/ruff', 'check', '--no-cache', '--isolated', '--']
    elif command.tool == 'python':
        prefix = ('-I', '-B', '-m', 'pytest')
        argv = ['/usr/bin/python3', *prefix, '-c', '/dev/null', '-p', 'no:cacheprovider',
                f'--basetemp={GUEST_SCRATCH}/pytest', '--']
    else:
        raise Refused('CHECK_COMMAND_POLICY')
    paths = command.arguments[len(prefix):]
    if command.arguments[:len(prefix)] != prefix or not paths:
        raise Refused('CHECK_COMMAND_POLICY')
    for path in paths:
        relative = PurePosixPath(path)
        if (not re.fullmatch(r'[a-zA-Z0-9_.][a-zA-Z0-9_./-]*', path) or str(relative) != path
                or '..' in relative.parts or '.git' in relative.parts or relative.is_absolute()):
            raise Refused('CHECK_COMMAND_PATH')
    return [*argv, *paths]


@dataclass(frozen=True)
class CheckInstallation:
    installation_id: str
    host_id: str
    boot_id: str
    kernel: str
    systemd_version: str
    owner_state: str
    systemctl: str
    bwrap: str
    controller_image_sha256: str
    runtime_image_sha256: str
    template_sha256: str
    systemctl_sha256: str
    bwrap_sha256: str
    service_limits: tuple
    maximum_scratch_bytes: int

    service_template = TEMPLATE
    controller_python = PYTHON
    controller_release = ENTRY
    service_user = 'hermes-symphony'
    service_group = 'hermes-symphony'


class CheckManager(SystemdManager):
    unit_prefix = PREFIX

    def __init__(self, qualification, state, *, deadline=None):
        super().__init__(qualification, state)
        self.deadline = deadline

    def _run(self, *args):
        timeout = 10 if self.deadline is None else min(10, self.deadline-time.monotonic())
        if timeout <= 0:
            raise TimeoutError('CHECK_START_TIMEOUT')
        return super()._run(*args, timeout=timeout)

    def verify_configuration(self, unit):
        super().verify_configuration(unit)
        fields = ('PrivateNetwork', 'NoNewPrivileges', 'CapabilityBoundingSet', 'AmbientCapabilities', 'UMask')
        raw = self._run('show', '--all', '--property=' + ','.join(fields), unit)
        if dict(line.split('=', 1) for line in raw.splitlines()) != dict(
                zip(fields, ('yes', 'yes', '', '', '0077'))):
            raise Refused('CHECK_SERVICE_BOUNDARY')


@dataclass(frozen=True)
class CheckConfiguration:
    installation: CheckInstallation
    profile: LinuxCheckProfile

    manifest = MANIFEST
    evidence = EVIDENCE
    runtime = RUNTIME
    state = STATE
    scratch = SCRATCH
    checks = CHECKS
    prefix = PREFIX
    installation_type = CheckInstallation
    git_project = False

    @property
    def invocation_profile(self):
        return self.profile.fingerprint

    @property
    def policy_digest(self):
        profile = asdict(self.profile)
        del profile['evidence_sha256']
        return digest({'schema': 1, 'installation': asdict(self.installation), 'profile': profile})

    @classmethod
    def load(cls, manifest=None):
        manifest = cls.manifest if manifest is None else manifest
        if str(manifest) != cls.manifest:
            raise Refused('CHECK_MANIFEST_PATH')
        raw = strict_json(protected(manifest))
        if type(raw) is not dict or set(raw) != {'schema', 'installation', 'profile'} or raw['schema'] != 1:
            raise Refused('CHECK_MANIFEST')
        system, profile = dict(raw['installation']), dict(raw['profile'])
        system['service_limits'] = tuple(system['service_limits'])
        profile['commands'] = tuple(CheckCommand(**{**cmd, 'arguments': tuple(cmd['arguments'])})
                                    for cmd in profile['commands'])
        result = cls(cls.installation_type(**system), LinuxCheckProfile(**profile))
        result.profile.validate()
        for cmd in result.profile.commands:
            if result.git_project and not cmd.requires_git:
                raise Refused('CHECK_COMMAND_POLICY')
            if result.git_project or not cmd.requires_git:
                checked_arguments(cmd)
        return result

    def verify(self):
        s, p = self.installation, self.profile
        if (not all(sha(v, 64) for v in (s.installation_id, s.host_id, s.controller_image_sha256,
                s.runtime_image_sha256, s.template_sha256, s.systemctl_sha256, s.bwrap_sha256))
                or (s.host_id, s.boot_id, s.kernel) != (host_identity(s.installation_id), boot_identity(),
                                                       summary()['machine']['os_release'])
                or not service_user_matches(s.service_user, s.service_group) or p.root != self.scratch
                or not 0 < p.timeout <= 90 or type(s.maximum_scratch_bytes) is not int or s.maximum_scratch_bytes <= 0
                or s.service_limits != ('1s', '2min', '536870912', '128')):
            raise Refused('CHECK_INSTALLATION_BINDING')
        roots = tuple(private_directory(v) for v in (s.owner_state, self.state, self.scratch))
        if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(roots) for b in roots[i+1:]):
            raise Refused('CHECK_INSTALLATION_ROOTS')
        volume = os.statvfs(roots[2])
        if (not os.path.ismount(roots[2]) or volume.f_blocks * volume.f_frsize > s.maximum_scratch_bytes
                or any(r.stat().st_dev == roots[2].stat().st_dev for r in roots[:2])):
            raise Refused('CHECK_SCRATCH_BUDGET')
        for root, fingerprint in ((CONTROLLER, s.controller_image_sha256), (self.runtime, s.runtime_image_sha256)):
            path = protected(root, directory=True)
            if not os.statvfs(path).f_flag & os.ST_RDONLY:
                raise Refused('CHECK_MUTABLE_RUNTIME')
            verify_image(path, fingerprint)
        for path, fingerprint in ((s.service_template, s.template_sha256), (s.bwrap, s.bwrap_sha256),
                                  (s.systemctl, s.systemctl_sha256), (p.git, p.git_sha256), (self.evidence, p.evidence_sha256)):
            if file_digest(path) != fingerprint:
                raise Refused('CHECK_QUALIFIED_BYTES_CHANGED')
        self.verify_evidence(strict_json(protected(self.evidence)))
        for entry in (PYTHON, ENTRY):
            protected(entry)
        for binary in ('/usr/bin/ruff', '/usr/bin/python3'):
            protected(in_image(self.runtime, binary))
        if self.git_project:
            protected(in_image(self.runtime, '/usr/bin/git'))
        elif any(os.path.lexists(in_image(self.runtime, name)) for name in
                 ('/usr/bin/git', '/bin/git', '/usr/libexec/git-core', '/usr/lib/git-core')):
            raise Refused('CHECK_IMAGE_CONTAINS_GIT')
        if not {'cpu', 'memory', 'pids'} <= set(Path('/sys/fs/cgroup/cgroup.controllers').read_text(encoding='ascii').split()):
            raise Refused('CHECK_SERVICE_CONTROLLERS')

    def verify_evidence(self, evidence):
        s = self.installation
        if (set(evidence) != {'schema', 'host', 'boot', 'policy', 'receipts'} or evidence['schema'] != 1
                or (evidence['host'], evidence['boot'], evidence['policy']) != (s.host_id, s.boot_id, self.policy_digest)
                or set(evidence['receipts']) != set(self.checks)
                or not all(sha(v, 64) for v in evidence['receipts'].values())):
            raise Refused('CHECK_VM_EVIDENCE')


GIT_MANIFEST = '/etc/hermes-symphony/git-check.json'
GIT_PREFIX = 'hermes-symphony-git-check@'


class GitCheckInstallation(CheckInstallation):
    service_template = '/etc/systemd/system/' + GIT_PREFIX + '.service'


class GitCheckConfiguration(CheckConfiguration):
    manifest = GIT_MANIFEST
    evidence = '/etc/hermes-symphony/git-check-evidence.json'
    runtime = '/opt/hermes-symphony/git-check-runtime'
    state = '/var/lib/hermes-symphony/git-check-control'
    scratch = '/var/lib/hermes-symphony/git-check-scratch'
    checks = tuple(item for item in CHECKS if item != 'no_git') + ('private_git_export', 'no_git_config')
    prefix = GIT_PREFIX
    installation_type = GitCheckInstallation
    git_project = True

    @property
    def invocation_profile(self):
        return digest(dict(kind='linux-git-check-v1', primary_profile=self.profile.launch_fingerprint,
                           evidence_sha256=self.profile.evidence_sha256))


class GitCheckManager(CheckManager):
    unit_prefix = GIT_PREFIX
