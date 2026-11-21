"""Owner-supplied qualification of a read-only Linux runtime and its concrete command boundary."""
from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import os
import re
from pathlib import Path, PurePosixPath
import stat

from hermes_cli.plugin_request_binding import canonical_home
from hermes_platform.host.summary import summary
from .linux_host import boot_identity, host_identity, service_user_matches
from .types import Refused, digest, identifier, sha


def protected(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise Refused('LINUX_RELEASE_PATH')
    for component in (path, *path.parents):
        info = component.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise Refused('LINUX_RELEASE_WRITABLE')
    if not (stat.S_ISDIR if directory else stat.S_ISREG)(path.stat().st_mode):
        raise Refused('LINUX_RELEASE_TYPE')
    return path


def file_digest(path):
    with protected(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@lru_cache(maxsize=4)
def _image_digest(path, dev, ino, size, change_ns):
    # Only administrator-owned backing files can populate this cache. Replacement or any
    # metadata/content change makes the stat tuple a new key.
    return file_digest(path)


def verify_image(root, expected_hash):
    exact = None
    for line in Path('/proc/self/mountinfo').read_bytes().splitlines():
        left, right = line.split(b' - ', 1)
        fields = left.split()
        name = re.sub(rb'\\([0-7]{3})', lambda m: bytes([int(m[1], 8)]), fields[4])
        mount = Path(os.fsdecode(name))
        if root in mount.parents:
            raise Refused('LINUX_RUNTIME_SUBMOUNT')
        if mount == root:
            if exact is not None or fields[3] != b'/' or right.split()[0] != b'squashfs':
                raise Refused('LINUX_RUNTIME_IMAGE')
            exact = fields[2].decode('ascii')
    if exact is None or not re.fullmatch(r'[0-9]+:[0-9]+', exact):
        raise Refused('LINUX_RUNTIME_IMAGE')
    loop = Path('/sys/dev/block')/exact/'loop'
    backing = protected(Path(os.fsdecode((loop/'backing_file').read_bytes()).rstrip('\n')))
    info = backing.stat()
    if _image_digest(backing, info.st_dev, info.st_ino, info.st_size, info.st_ctime_ns) != expected_hash:
        raise Refused('LINUX_RUNTIME_IMAGE_CHANGED')


def in_image(root, name):
    value = PurePosixPath(name)
    if not value.is_absolute() or '..' in value.parts or str(value) != name:
        raise Refused('LINUX_RUNTIME_PATH')
    return Path(root)/name.lstrip('/')


# no-tmp: ok — this is the qualified tmpfs mount inside the read-only guest image.
GUEST_SCRATCH = '/tmp'
GUEST_HOME = GUEST_SCRATCH + '/home'
ENVIRONMENT = {'HOME': GUEST_HOME, 'TMPDIR': GUEST_SCRATCH, 'PATH': '/usr/bin:/bin', 'LANG': 'C',
               'CODEX_HOME': '/etc/symphony/codex'}
BOUNDARY_CHECKS = frozenset(('write_workspace', 'write_tmp', 'deny_network', 'deny_control',
                             'deny_journal', 'deny_neighbor', 'deny_source', 'deny_credentials',
                             'deny_runtime_write', 'deny_cgroup', 'deny_owner_processes'))


@dataclass(frozen=True)
class LinuxQualification:
    profile_home: str
    namespace: str
    scheduler_id: str
    issuer_refs: tuple
    installation_id: str
    host_id: str
    boot_id: str
    kernel: str
    environment_ref: str
    permission_profile: str
    runtime: str
    executable: str
    probe_python: str
    service_template: str
    controller_release: str
    controller_root: str
    controller_python: str
    controller_image_sha256: str
    service_user: str
    service_group: str
    systemd_version: str
    systemctl: str
    bwrap: str
    # Hashes cover policy, launcher, client, runtime image, systemd manager and test runtime.
    hashes: tuple
    evidence_sha256: str
    command_environment: tuple
    settings_sha256: str
    marker_set_sha256: str
    image_sha256: str
    image_device: int
    image_inode: int
    service_limits: tuple

    def validate(self):
        if (self.profile_home != canonical_home(self.profile_home) or not Path(self.profile_home).is_absolute()
                or not all(identifier(x) for x in (self.namespace, self.environment_ref, self.scheduler_id,
                                                  self.permission_profile))
                or not self.issuer_refs or len(set(self.issuer_refs)) != len(self.issuer_refs)
                or not all(identifier(x) for x in self.issuer_refs)
                or not all(sha(x, 64) for x in (self.evidence_sha256, self.host_id, self.installation_id))
                or not sha(self.boot_id, 32) or not self.kernel or not sha(self.marker_set_sha256, 64)
                or not all(sha(v, 64) for v in (self.image_sha256, self.controller_image_sha256, self.settings_sha256))
                or not all(identifier(x) and x not in ('0', 'root') for x in (self.service_user, self.service_group))
                or not self.systemd_version
                or not self.command_environment or tuple(sorted(set(self.command_environment))) != self.command_environment
                or not set(self.command_environment) <= ENVIRONMENT.keys()
                or self.image_device <= 0 or self.image_inode <= 0 or len(self.service_limits) != 4
                or not all(type(v) is str and v not in ('', 'infinity', '[not set]', '0')
                           for v in self.service_limits)):
            raise Refused('LINUX_QUALIFICATION')

    def verify(self):
        self.validate()
        if (host_identity(self.installation_id), boot_identity(), summary()['machine']['os_release']) != (
                self.host_id, self.boot_id, self.kernel):
            raise Refused('LINUX_QUALIFICATION_HOST')
        if not service_user_matches(self.service_user, self.service_group):
            raise Refused('LINUX_DEDICATED_SERVICE_USER')
        controller = protected(self.controller_root, directory=True)
        python, entry = protected(self.controller_python), protected(self.controller_release)
        if (controller not in python.parents or controller not in entry.parents
                or entry.parts[-4:] != ('plugins', 'symphony_pm', 'transport', 'linux_entry.py')
                or not os.statvfs(controller).f_flag & os.ST_RDONLY):
            raise Refused('LINUX_CONTROLLER_RELEASE')
        verify_image(controller, self.controller_image_sha256)
        root = protected(self.runtime, directory=True)
        image = root.stat()
        if ((image.st_dev, image.st_ino) != (self.image_device, self.image_inode)
                or not os.statvfs(root).f_flag & os.ST_RDONLY):
            raise Refused('LINUX_RUNTIME_MUTABLE')
        verify_image(root, self.image_sha256)
        files = (self.service_template, self.controller_release, self.systemctl, self.bwrap,
                 in_image(root, self.executable), in_image(root, self.probe_python),
                 in_image(root, '/etc/symphony/codex/config.toml'))
        if len(self.hashes) != len(files) or tuple(file_digest(p) for p in files) != self.hashes:
            raise Refused('LINUX_QUALIFIED_BYTES_CHANGED')

    @property
    def fingerprint(self):
        return digest(asdict(self))


def verify_report(report, qualification, challenge, owner_net):
    if (type(report) is not dict or set(report) != {'challenge', 'environment', 'checks', 'network_namespace', 'interfaces'}
            or type(owner_net) is not str or not re.fullmatch(r'net:\[\d+\]', owner_net)
            or type(report['network_namespace']) is not str or not re.fullmatch(r'net:\[\d+\]', report['network_namespace'])
            or report['network_namespace'] == owner_net or report['interfaces'] != ['lo']
            or report['challenge'] != challenge or type(report['environment']) is not list
            or not all(type(k) is str for k in report['environment'])
            or tuple(sorted(report['environment'])) != qualification.command_environment
            or type(report['checks']) is not dict or set(report['checks']) != BOUNDARY_CHECKS
            or not all(v is True for v in report['checks'].values())):
        raise Refused('LINUX_PRECHECK_REFUSED')
    return digest(report)


def sandbox_command(spec, command):
    """Use only the immutable image, this attempt's tree and anonymous per-invocation tmpfs."""
    return [spec['bwrap'], '--unshare-all', '--new-session', '--die-with-parent',
            '--cap-drop', 'ALL', '--clearenv', '--ro-bind', spec['runtime'], '/',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', GUEST_SCRATCH, '--dir', GUEST_HOME,
            '--bind', spec['workspace'], '/work', '--chdir', '/work',
            *(arg for k, v in ENVIRONMENT.items() for arg in ('--setenv', k, v)), '--', *command]
