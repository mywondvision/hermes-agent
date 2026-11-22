"""Delegated service controller for one approved non-Git check. The payload never opens the spool."""
from dataclasses import asdict
import os
from pathlib import Path
import signal
import time

from .linux_check_config import CheckConfiguration, GIT_MANIFEST, GitCheckConfiguration, strict_json
from .linux_check_wire import CheckGroup, verify_copy, read_request, sandbox_argv, unit_name
from .linux_control import group_values, members, publish
from .linux_host import private_directory, process_identity, require_fork
from .types import Refused, digest, sha


def _payload(read_fd, write_fd, config, invocation, signal_fd):
    os.close(write_fd)
    try:
        if os.read(read_fd, 1) != b'1':
            return
        os.close(read_fd)
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 << 20, 16 << 20))
        with open(os.devnull, 'r+b', buffering=0) as null:
            for fd in (0, 1, 2):
                os.dup2(null.fileno(), fd)
        if signal_fd is not None:
            os.dup2(signal_fd, 1)
            os.close(signal_fd)
        argv = sandbox_argv(config, invocation)
        os.execve(argv[0], argv, {})
    finally:
        os._exit(125)


def _wait_grant(directory, bound, expires):
    expected = {'key': bound.key, 'binding': digest(asdict(bound))}
    while time.monotonic() < expires:
        try:
            if strict_json(directory/'grant') != expected:
                raise Refused('CHECK_SERVICE_GRANT')
            return True
        except FileNotFoundError:
            time.sleep(min(0.04, max(0.0, expires - time.monotonic())))
    return False


def _group_identity(fd, group, bound):
    fd_info, path_info = os.fstat(fd), group.stat()
    if (not os.path.samestat(fd_info, path_info) or (fd_info.st_dev, fd_info.st_ino) != (bound.device, bound.inode)
            or os.environ['INVOCATION_ID'] != bound.service_id):
        raise Refused('CHECK_CGROUP_CHANGED')


def _seal_and_run(directory, group, child, pipe, config, invocation, request):
    info = group.stat()
    root = process_identity(child)
    if root is None or '0::/' + str(group.relative_to('/sys/fs/cgroup')) not in os.fsdecode(Path(f'/proc/{child}/cgroup').read_bytes()).splitlines():
        raise Refused('CHECK_ROOT_MEMBERSHIP')
    bound = CheckGroup(invocation.key, invocation.profile, invocation.epoch, config.installation.host_id,
        config.installation.boot_id, unit_name(invocation.key, config.prefix), os.environ['INVOCATION_ID'],
        digest(request), '/' + str(group.relative_to('/sys/fs/cgroup')), info.st_dev, info.st_ino, root)
    CheckGroup.parse(asdict(bound), invocation, config)
    publish(directory, 'ready', asdict(bound))
    release = 'never_granted'
    if _wait_grant(directory, bound, request['expires']):
        if type(config).load() != config:
            raise Refused('CHECK_MANIFEST_CHANGED')
        config.verify()
        verify_copy(config, invocation)
        if time.monotonic() < request['expires']:
            os.write(pipe, b'1')
            release = 'started'
    # Closing the only pipe prohibits any second root. No worker may migrate into this leaf.
    return bound, release


def _proof(bound, observed, known, code, timed_out, release):
    processes = []
    for p in sorted(known, key=lambda proc: proc.pid):
        if process_identity(p.pid) is not None:
            # Reused PID is ambiguous for this receipt. Never promote it to original death.
            raise Refused('CHECK_PROCESS_STOP_UNKNOWN')
        processes.append({**asdict(p), 'gone': True})
    return {'schema': 1, 'binding': digest(asdict(bound)), 'release': release, 'sealed': True, 'populated': 0, 'processes': processes,
            'observed': observed, 'returncode': code, 'timed_out': timed_out}


def _term_bound(fd, group, bound):
    for p in members(fd, bound.cgroup):
        try:
            pidfd = os.pidfd_open(p.pid)
        except ProcessLookupError:
            continue
        try:
            if process_identity(p.pid) == p and p in members(fd, bound.cgroup):
                _group_identity(fd, group, bound)
                signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        except ProcessLookupError:
            continue
        finally:
            os.close(pidfd)


def _kill_bound(fd, group, bound):
    kill_fd = os.open('cgroup.kill', os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        _group_identity(fd, group, bound)
        if os.write(kill_fd, b'1') != 1:
            raise Refused('CHECK_GROUP_STOP_UNPROVEN')
    finally:
        os.close(kill_fd)


def supervise(directory, group, fd, bound, request, release, signal_fd=None):
    timed_out = killed = False
    sent_at = code = None
    known = {bound.root}
    while True:
        _group_identity(fd, group, bound)
        known.update(members(fd, bound.cgroup))
        if len(known) > 8192:
            raise Refused('CHECK_MEMBER_LIMIT')
        now = time.monotonic()
        if code is None:
            pid, status = os.waitpid(bound.root.pid, os.WNOHANG)
            if pid:
                code = os.waitstatus_to_exitcode(status)
        if now >= request['expires']:
            timed_out = True
        if code is not None and group_values(fd) == 0:
            if signal_fd is not None and (os.read(signal_fd, 2) != b'G' or code not in (0, 1)):
                code = -125  # The trusted probe or tool setup did not yield a project verdict.
            proof = _proof(bound, now, known, code, timed_out, release)
            _group_identity(fd, group, bound)
            if group_values(fd) != 0:
                raise Refused('CHECK_CGROUP_CHANGED')
            publish(directory, 'stop', proof)
            return
        if (code is not None or timed_out) and sent_at is None:
            _term_bound(fd, group, bound)
            sent_at = now
        if sent_at is not None and not killed and now-sent_at >= 1:
            _kill_bound(fd, group, bound)
            killed = True
        if sent_at is not None and now-sent_at > 8:
            raise Refused('CHECK_GROUP_STOP_UNPROVEN')
        time.sleep(0.04)


def serve(manifest, key):
    fork = require_fork()
    kind = GitCheckConfiguration if manifest == GIT_MANIFEST else CheckConfiguration
    config = kind.load(manifest)
    config.verify()
    unit = unit_name(key, config.prefix)
    directory = private_directory(private_directory(config.state)/unit)
    invocation, request = read_request(directory, key, config)
    if not sha(os.environ.get('INVOCATION_ID'), 32) or not request['started'] <= time.monotonic() < request['expires']:
        raise Refused('CHECK_SERVICE_START_EXPIRED')
    path = os.fsdecode(Path('/proc/self/cgroup').read_bytes()).strip().split('::')
    if len(path) != 2 or path[0] != '0' or Path(path[1]).name != unit:
        raise Refused('CHECK_SERVICE_CGROUP')
    group = Path('/sys/fs/cgroup')/path[1].lstrip('/')/'payload'
    group.mkdir()
    if not (group/'cgroup.kill').is_file():
        raise Refused('CHECK_SERVICE_CGROUP')
    verify_copy(config, invocation)
    fd = os.open(group, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    signal_read, signal_write = os.pipe() if config.git_project else (None, None)
    try:
        read_fd, write_fd = os.pipe()
        child = fork()
        if child == 0:
            if signal_read is not None:
                os.close(signal_read)
            _payload(read_fd, write_fd, config, invocation, signal_write)
        if signal_write is not None:
            os.close(signal_write)
        os.close(read_fd)
        try:
            (group/'cgroup.procs').write_text(str(child), encoding='ascii')
            bound, release = _seal_and_run(directory, group, child, write_fd, config, invocation, request)
        finally:
            os.close(write_fd)
        supervise(directory, group, fd, bound, request, release, signal_read)
    finally:
        if signal_read is not None:
            os.close(signal_read)
        os.close(fd)
