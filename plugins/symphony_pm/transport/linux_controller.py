"""Entry point for an administrator-installed, non-restarting systemd attempt template.

Template ExecStart invokes this module with the absolute owner state directory and %i.
The service runs as the dedicated gateway user with a delegated cgroup v2 subtree.
Only the controller touches that subtree. Neither sandbox receives the state directory.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .linux_control import message, publish
from .linux_carrier import refuse_primary_dispatch
from .linux_host import private_directory, process_identity, require_fork
from .linux_sandbox import sandbox_command
from .types import Refused, sha


def _gate(directory, name, invocation):
    while True:
        if (directory/'cancel').exists():
            raise Refused('LINUX_CANCELLED')
        try:
            if message(directory, name) != invocation:
                raise Refused('LINUX_GATE_BINDING')
            return
        except FileNotFoundError:
            time.sleep(0.04)


def _payload(directory, spec, invocation):
    if 'carrier' in spec:
        refuse_primary_dispatch()
    import resource
    limit = 16 << 20
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    _gate(directory, 'probe', invocation)
    with (directory/'probe-output').open('xb') as out:
        result = subprocess.run(sandbox_command(spec, spec['probe_argv']), stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.DEVNULL, env={}, close_fds=True,
                                timeout=spec['probe_timeout'], check=False)
    publish(directory, 'probe-exit', result.returncode)
    _gate(directory, 'exec', invocation)
    with (directory/'stdin').open('rb') as src, (directory/'stdout').open('xb') as out, (
            directory/'stderr').open('xb') as err:
        os.dup2(src.fileno(), 0)
        os.dup2(out.fileno(), 1)
        os.dup2(err.fileno(), 2)
        command = sandbox_command(spec, spec['argv'])
        os.execve(command[0], command, {})


def _term_group(group):
    # Matching the PID after pidfd_open prevents signalling a recycled number outside the group.
    expected = '/' + str(group.relative_to('/sys/fs/cgroup'))
    for line in (group/'cgroup.procs').read_text(encoding='ascii').splitlines():
        pid = int(line)
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            if ('0::' + expected) in os.fsdecode(Path(f'/proc/{pid}/cgroup').read_bytes()).splitlines():
                signal.pidfd_send_signal(fd, signal.SIGTERM)
        except (ProcessLookupError, FileNotFoundError):
            pass
        finally:
            os.close(fd)


def _supervise(directory, group, invocation, child):
    sent_at = None
    killed = False
    exit_recorded = False
    while True:
        if not exit_recorded:
            pid, status = os.waitpid(child, os.WNOHANG)
            if pid:
                publish(directory, 'exit', os.waitstatus_to_exitcode(status))
                exit_recorded = True
        cancelled = (directory/'cancel').exists()
        if (cancelled or (directory/'exec').exists() or exit_recorded) and not (directory/'sealed').exists():
            publish(directory, 'sealed', invocation)
        if cancelled and sent_at is None:
            if message(directory, 'cancel') != invocation:
                raise Refused('LINUX_CANCEL_BINDING')
            _term_group(group)
            sent_at = time.monotonic()
        if sent_at is not None and not killed and time.monotonic() - sent_at >= 1:
            (group/'cgroup.kill').write_text('1', encoding='ascii')
            killed = True
        if exit_recorded and (directory/'retire').exists() and message(directory, 'retire') == invocation:
            return
        time.sleep(0.04)


def serve(state, instance):
    fork = require_fork()
    if not sha(instance, 64):
        raise Refused('LINUX_SERVICE_INSTANCE')
    unit = f'hermes-symphony@{instance}.service'
    directory = private_directory(private_directory(state)/unit)
    invocation = os.environ['INVOCATION_ID']
    if not sha(invocation, 32):
        raise Refused('LINUX_INVOCATION')
    tag, path = os.fsdecode(Path('/proc/self/cgroup').read_bytes()).strip().split('::')
    if tag != '0' or Path(path).name != unit:
        raise Refused('LINUX_DELEGATION')
    group = Path('/sys/fs/cgroup') / path.lstrip('/') / 'payload'
    group.mkdir()
    if not (group/'cgroup.kill').is_file():
        raise Refused('LINUX_CGROUP_KILL_UNAVAILABLE')
    spec = message(directory, 'spec')
    read_fd, write_fd = os.pipe()
    child = fork()
    if child == 0:
        os.close(write_fd)
        try:
            if os.read(read_fd, 1) != b'1':
                os._exit(125)
            os.close(read_fd)
            _payload(directory, spec, invocation)
        finally:
            os._exit(125)
    os.close(read_fd)
    try:
        (group/'cgroup.procs').write_text(str(child), encoding='ascii')
        if child not in tuple(map(int, (group/'cgroup.procs').read_text(encoding='ascii').split())):
            raise Refused('LINUX_ROOT_MEMBERSHIP')
        info = group.stat()
        root = process_identity(child)
        publish(directory, 'ready', {'invocation_id': invocation, 'cgroup_path': path+'/payload',
                                      'device': info.st_dev, 'inode': info.st_ino,
                                      'root': {'pid': root.pid, 'creation': root.creation}})
        os.write(write_fd, b'1')
    finally:
        os.close(write_fd)
    _supervise(directory, group, invocation, child)


if __name__ == '__main__':
    serve(*sys.argv[1:])
