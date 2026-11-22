"""The durable, attempt-bound records shared by the check controller and its read-only observer."""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path, PurePosixPath

from .linux_check_config import PREFIX, checked_arguments, strict_json
from .linux_check_host import LinuxCheckInvocation, LinuxCheckTerminal
from .linux_check_probe import GIT_ENV, GIT_PROGRAM, PROGRAM
from .linux_sandbox import GUEST_HOME, GUEST_SCRATCH
from .recapture import directory_identity
from .types import ProcessIdentity, Refused, digest, finite, sha


def unit_name(key, prefix=PREFIX):
    if not sha(key, 64):
        raise Refused('CHECK_SERVICE_INSTANCE')
    return prefix + key + '.service'


def read_request(directory, key, config):
    raw = strict_json(directory/'request')
    if type(raw) is not dict or set(raw) != {'schema', 'invocation', 'key', 'started', 'expires'} or raw['schema'] != 1:
        raise Refused('CHECK_SERVICE_REQUEST')
    if config.git_project:
        from .linux_robot_check import GitCheckInvocation
        invocation = GitCheckInvocation.from_record(raw['invocation'])
    else:
        invocation = LinuxCheckInvocation.from_record(raw['invocation'])
    if (raw['key'] != invocation.key or key != invocation.key or invocation.profile != config.invocation_profile
            or invocation.command not in config.profile.commands or not finite(raw['started']) or not finite(raw['expires'])
            or not 0 < raw['expires'] - raw['started'] <= config.profile.timeout + 0.001):
        raise Refused('CHECK_SERVICE_REQUEST')
    checked_arguments(invocation.command)
    return invocation, raw


def check_copy(invocation, profile):
    src = Path(invocation.directory)
    if (src.name != 'src' or src.parent.parent != Path(profile.root) or not src.parent.name.startswith('c-')
            or src.resolve(strict=True) != src or tuple(directory_identity(src)) != invocation.directory_id
            or tuple(directory_identity(src.parent)) != invocation.scratch_id):
        raise Refused('CHECK_SERVICE_COPY')


def verify_copy(config, invocation):
    if not config.git_project:
        return check_copy(invocation, config.profile)
    src = Path(invocation.directory)
    expected = Path(config.profile.root)/('g-' + invocation.nonce)/'src'
    if src != expected or src.resolve(strict=True) != src or tuple(directory_identity(src)) != invocation.directory_id:
        raise Refused('CHECK_SERVICE_COPY')


def sandbox_argv(config, invocation):
    verify_copy(config, invocation)
    # Commands use absolute paths. Project checks also resolve Git in the measured image.
    # Writable space is a private tmpfs. The input is mounted read-only.
    namespaces = {kind: os.readlink('/proc/self/ns/' + kind) for kind in ('net', 'pid', 'mnt')}
    hidden = (config.state, config.installation.owner_state, config.profile.root, str(Path(invocation.directory).parent),
              str(Path(config.installation.owner_state)/'owner-check.active'), '/sys/fs/cgroup/cgroup.procs')
    env = [item for k, v in GIT_ENV.items() for item in ('--setenv', k, v)] if config.git_project else []
    program = GIT_PROGRAM if config.git_project else PROGRAM
    target = [invocation.commit, invocation.source_sha] if config.git_project else []
    return [config.installation.bwrap, '--unshare-all', '--disable-userns', '--new-session', '--die-with-parent',
            '--cap-drop', 'ALL',
            '--clearenv', '--ro-bind', config.runtime, '/', '--proc', '/proc', '--dev', '/dev', '--tmpfs', GUEST_SCRATCH,
            '--dir', GUEST_HOME, '--ro-bind', invocation.directory, '/work', '--chdir', '/work',
            '--setenv', 'HOME', GUEST_HOME, '--setenv', 'TMPDIR', GUEST_SCRATCH, '--setenv', 'PATH', '/usr/bin' if config.git_project else '/nonexistent',
            '--setenv', 'LANG', 'C', '--setenv', 'LC_ALL', 'C', '--setenv', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1',
            *env, '--', '/usr/bin/python3', '-I', '-B', '-c', program, json.dumps(namespaces), json.dumps(hidden), *target,
            *checked_arguments(invocation.command)]


@dataclass(frozen=True)
class CheckGroup:
    key: str
    profile: str
    epoch: int
    host: str
    boot: str
    unit: str
    service_id: str
    request_sha256: str
    cgroup: str
    device: int
    inode: int
    root: ProcessIdentity

    @classmethod
    def parse(cls, record, invocation, config):
        body = dict(record)
        body['root'] = ProcessIdentity(**body['root'])
        bound = cls(**body)
        path = PurePosixPath(bound.cgroup)
        install = config.installation
        if ((bound.key, bound.profile, bound.epoch, bound.host, bound.boot, bound.unit) != (
                invocation.key, invocation.profile, invocation.epoch, install.host_id,
                install.boot_id, unit_name(invocation.key, config.prefix))
                or type(bound.epoch) is not int or not sha(bound.service_id, 32) or not sha(bound.request_sha256, 64)
                or not path.is_absolute() or str(path) != bound.cgroup or '..' in path.parts
                or path.parts[-2:] != (bound.unit, 'payload') or not bound.root.valid()
                or not bound.root.creation.isdecimal() or type(bound.device) is not int or bound.device <= 0
                or type(bound.inode) is not int or bound.inode <= 0):
            raise Refused('CHECK_SERVICE_BINDING')
        return bound


def terminal_from_proof(proof, invocation, bound, request):
    fields = {'schema', 'binding', 'release', 'sealed', 'populated', 'processes', 'observed', 'returncode', 'timed_out'}
    if (type(proof) is not dict or set(proof) != fields or type(proof['schema']) is not int or proof['schema'] != 1
            or bound.request_sha256 != digest(request) or proof['binding'] != digest(asdict(bound))
            or proof['release'] not in ('never_granted', 'started') or proof['sealed'] is not True
            or type(proof['populated']) is not int or proof['populated'] != 0
            or not finite(proof['observed']) or proof['observed'] < request['started']
            or type(proof['returncode']) is not int or not -127 <= proof['returncode'] <= 255
            or type(proof['timed_out']) is not bool or not isinstance(proof['processes'], list)):
        raise Refused('CHECK_GROUP_STOP_UNPROVEN')
    if (proof['release'] == 'never_granted' and (not proof['timed_out'] or proof['returncode'] == 0)
            or proof['observed'] >= request['expires'] and not proof['timed_out']):
        raise Refused('CHECK_GROUP_STOP_UNPROVEN')
    processes = []
    for row in proof['processes']:
        if type(row) is not dict or set(row) != {'pid', 'creation', 'gone'} or row['gone'] is not True:
            raise Refused('CHECK_GROUP_STOP_UNPROVEN')
        identity = ProcessIdentity(row['pid'], row['creation'])
        if not identity.valid() or not identity.creation.isdecimal():
            raise Refused('CHECK_GROUP_STOP_UNPROVEN')
        processes.append(identity)
    if bound.root not in processes or len({p.pid for p in processes}) != len(processes):
        raise Refused('CHECK_GROUP_STOP_UNPROVEN')
    return LinuxCheckTerminal(invocation.key, invocation.profile, invocation.epoch, proof['returncode'],
                              proof['timed_out'], digest(proof))
