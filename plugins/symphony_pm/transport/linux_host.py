"""Linux-only host ownership and read-only systemd/cgroup discovery.

A dedicated host installation supplies one persistent, owner-private state directory.
The persistent scope stamp also fences a different profile after the writer dies.
"""
import os
import re
from pathlib import Path
import stat
import subprocess
import uuid

from .linux_identity import CONTRACT
from .recovery import PredecessorProof
from .types import ProcessIdentity, Refused, ServingIdentity, digest, sha


def effective_ids():
    uid = getattr(os, 'geteuid', None)
    gid = getattr(os, 'getegid', None)
    if uid is None or gid is None:
        raise Refused('LINUX_EFFECTIVE_ID_UNAVAILABLE')
    return uid(), gid()


def service_user_matches(user, group):
    uid, gid = effective_ids()
    import grp
    import pwd
    return uid != 0 and gid != 0 and pwd.getpwuid(uid).pw_name == user and grp.getgrgid(gid).gr_name == group


def require_fork():
    fork = getattr(os, 'fork', None)
    if fork is None:
        raise Refused('LINUX_FORK_UNAVAILABLE')
    return fork


def private_directory(path):
    path = Path(path)
    uid, _ = effective_ids()
    info = path.lstat()
    if (not path.is_absolute() or path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid or info.st_mode & 0o077):
        raise Refused('LINUX_OWNER_DIRECTORY')
    for parent in path.parents:
        info = parent.lstat()
        if info.st_uid not in (0, uid) or info.st_mode & 0o022:
            raise Refused('LINUX_OWNER_DIRECTORY_PARENT')
    return path


def boot_identity():
    return uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()).hex


def process_identity(pid):
    try:
        raw = Path('/proc', str(pid), 'stat').read_bytes()
    except FileNotFoundError:
        return None
    # comm may contain spaces and ')' (even a newline); field 22 is starttime.
    tail = raw[raw.rindex(b')') + 2:].split()
    ticks = tail[19].decode('ascii')
    if not ticks.isdecimal() or not raw.startswith(str(pid).encode() + b' ('):
        raise Refused('LINUX_PROC_IDENTITY')
    return ProcessIdentity(pid, ticks)


def host_identity(installation_id):
    machine = Path('/etc/machine-id').read_text(encoding='ascii').strip()
    if not sha(machine, 32) or not sha(installation_id, 64):
        raise Refused('LINUX_HOST_IDENTITY')
    return digest([machine, installation_id])


class LinuxOwnershipGuard:
    def __init__(self, scope, state, installation_id):
        import fcntl
        state = private_directory(state)
        self.pid = os.getpid()
        self.recovery_host_id = host_identity(installation_id)
        self.identity = ServingIdentity(scope.profile_home, scope.namespace, boot_identity(),
                                        process_identity(self.pid), uuid.uuid4().hex)
        self.path = state/'host-owner.lock'
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stamp = digest([scope.profile_home, scope.namespace, self.recovery_host_id]).encode('ascii')
            prior = os.read(self.fd, 128)
            if prior and prior != stamp:
                raise Refused('LINUX_HOST_SCOPE')
            if not prior:
                if os.write(self.fd, stamp) != len(stamp):
                    raise Refused('LINUX_HOST_SCOPE')
                os.fsync(self.fd)
            self.assert_owned()
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def assert_owned(self):
        if self.fd is None or os.getpid() != self.pid:
            raise Refused('WRITER_FENCED')
        uid, _ = effective_ids()
        info, current = os.fstat(self.fd), self.path.lstat()
        if (not os.path.samestat(info, current) or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or info.st_uid != uid or info.st_mode & 0o077):
            raise Refused('WRITER_FENCED')

    def require_predecessor_dead(self, previous):
        self.assert_owned()
        process = ProcessIdentity(**previous['process'])
        if process_identity(process.pid) == process:
            raise Refused('PREDECESSOR_LIVE')

    def predecessor_proof(self, previous):
        self.assert_owned()
        if previous['boot_id'] != self.identity.boot_id:
            # Local machine and disk IDs do not rule out a snapshot rollback or a clone.
            return None
        self.require_predecessor_dead(previous)
        return PredecessorProof(digest(previous), self.recovery_host_id, 'OWNER_DEAD',
                                'linux-flock-and-proc-starttime', CONTRACT)

    def close(self, deadline):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


# Identifies the source-distributed unit among templates pinned by an administrator.
PRIMARY_TEMPLATE_SHA256 = '1fbd10ca27fbfeadf3b7affdec5edeb21635ec5fe8a64e5b1394f7902365ff57'


class SystemdManager:
    unit_prefix = 'hermes-symphony@'

    def __init__(self, qualification, state):
        q = qualification
        self.executable, self.template, self.limits = q.systemctl, q.service_template, q.service_limits
        self.entry, self.python, self.state = q.controller_release, q.controller_python, str(state)
        self.user, self.group, self.version = q.service_user, q.service_group, q.systemd_version
        self.packaged_primary = getattr(q, 'hashes', ())[:1] == (PRIMARY_TEMPLATE_SHA256,)

    def _run(self, *args, timeout=10):
        done = subprocess.run([self.executable, '--no-pager', '--no-ask-password', *args],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, timeout=timeout, check=False)
        if done.returncode:
            raise Refused('LINUX_MANAGER_UNKNOWN')
        return done.stdout.decode('utf-8')

    def start(self, unit):
        self.verify_configuration(unit)
        self._run('start', '--no-block', unit)

    def verify_configuration(self, unit):
        instance = unit.removeprefix(self.unit_prefix).removesuffix('.service')
        if (not sha(instance, 64) or unit != self.unit_prefix + instance + '.service'
                or not all(re.fullmatch(r'/[\w./-]+', path) for path in (self.python, self.entry, self.state))):
            raise Refused('LINUX_UNIT_CONFIGURATION')
        # The source-distributed primary unit has an explicit service boundary. Other
        # administrator-qualified layouts keep their own measured template and profile.
        expected = {'Id': unit, 'FragmentPath': self.template, 'DropInPaths': '', 'NeedDaemonReload': 'no',
                    'Restart': 'no', 'KillMode': 'control-group', 'Delegate': 'yes', 'User': self.user,
                    'Group': self.group, 'ExecStartPre': '', 'ExecStartPost': '', 'ExecCondition': ''}
        if self.packaged_primary:
            expected.update(Type='exec', UMask='0077', PrivateNetwork='yes', NoNewPrivileges='yes',
                            CapabilityBoundingSet='', AmbientCapabilities='', TimeoutStopUSec='8s',
                            ExecReload='', ExecStop='', ExecStopPost='',
                            StandardInput='null', StandardOutput='null', StandardError='journal')
        expected.update(zip(('CPUQuotaPerSecUSec', 'RuntimeMaxUSec', 'MemoryMax', 'TasksMax'), self.limits,
                            strict=True))
        fields = (*expected, 'ExecStart')
        lines = self._run('show', '--all', '--property=' + ','.join(fields), unit).splitlines()
        if len(lines) != len(fields) or any('=' not in line for line in lines):
            raise Refused('LINUX_UNIT_CONFIGURATION')
        values = dict(line.split('=', 1) for line in lines)
        start = values.pop('ExecStart', '')
        argv = ' '.join((self.python, '-I', '-S', self.entry, self.state, instance))
        prefix = '{ path=' + self.python + ' ; argv[]=' + argv + ' ; ignore_errors=no ; '
        if (values != expected or not start.startswith(prefix)
                or start.count('argv[]=') != 1 or not start.endswith(' }')):
            raise Refused('LINUX_UNIT_CONFIGURATION')
        if self._run('show', '--property=Version', '--value').strip() != self.version:
            raise Refused('LINUX_SYSTEMD_VERSION')

    def describe(self, unit):
        fields = ('Id', 'InvocationID', 'ControlGroup', 'Restart', 'KillMode', 'Delegate',
                  'RuntimeMaxUSec', 'MemoryMax', 'TasksMax', 'MainPID', 'ActiveState')
        raw = self._run('show', '--all', '--property=' + ','.join(fields), unit)
        values = dict(line.split('=', 1) for line in raw.splitlines())
        if (set(values) != set(fields) or values['Id'] != unit or values['Restart'] != 'no'
                or values['KillMode'] != 'control-group' or values['Delegate'] != 'yes'
                or values['ActiveState'] not in ('activating', 'active')
                or any(values[key] in ('', '0', 'infinity', '[not set]')
                       for key in ('RuntimeMaxUSec', 'MemoryMax', 'TasksMax'))
                or not sha(values['InvocationID'], 32)):
            raise Refused('LINUX_UNIT_BOUNDARY')
        return values
