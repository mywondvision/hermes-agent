"""Attempt-private messages between a restricted systemd controller and the owning gateway."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from .linux_host import boot_identity, process_identity
from .linux_identity import LinuxBinding, LinuxStopReadback, unit_for
from .lifecycle import ObservationTarget, ObserverHandshake, ProcessReadback
from .types import ProcessIdentity, Refused, digest


def strict_json(path, limit=131072):
    info = path.lstat()
    # Capture metadata may contain all source paths. Its measured size still bounds a racing read.
    if limit is None:
        limit = info.st_size
    if (not stat.S_ISREG(info.st_mode) or info.st_size > limit or path.resolve(strict=True) != path):
        raise Refused('CHECK_RECORD_TYPE')
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise Refused('CHECK_RECORD_DUPLICATE')
        return result
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise Refused('CHECK_RECORD_SIZE')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique)


def _sync_directory(directory):
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def publish(directory, name, value):
    """A closed durable file appears exactly once, including across controller restarts."""
    fd, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=True, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.link(temporary, directory/name)
        _sync_directory(directory)
    finally:
        os.unlink(temporary)


def message(directory, name):
    with (directory/name).open(encoding='utf-8') as src:
        return json.load(src)


def wait_message(directory, name, deadline):
    while True:
        try:
            return message(directory, name)
        except FileNotFoundError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('LINUX_GATE_TIMEOUT')
            time.sleep(min(remaining, 0.04))


def group_values(fd):
    raw = os.open('cgroup.events', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    with os.fdopen(raw, encoding='ascii') as stream:
        values = dict(line.split() for line in stream)
    if values.get('populated') not in ('0', '1'):
        raise Refused('LINUX_CGROUP_EVENTS')
    return int(values['populated'])


def members(fd, path):
    """The controller creates a single payload leaf and grants its worker no delegation."""
    for entry in os.scandir(fd):
        if entry.is_dir(follow_symlinks=False):
            raise Refused('LINUX_UNEXPECTED_SUBGROUP')
    raw = os.open('cgroup.procs', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    with os.fdopen(raw, encoding='ascii') as stream:
        pids = {int(value) for value in stream}
    known = []
    for pid in pids:
        try:
            proc = process_identity(pid)
            if (proc is not None and '0::' + path in os.fsdecode(Path(f'/proc/{pid}/cgroup').read_bytes()).splitlines()
                    and process_identity(pid) == proc):
                known.append(proc)
        except FileNotFoundError:
            continue
    return tuple(sorted(known, key=lambda p: p.pid))


class LinuxObserver:
    def __init__(self, state, manager, host_id, *, clock=time.time):
        self.state, self.manager, self.host_id = Path(state), manager, host_id
        self.clock, self.targets = clock, {}
        self.closed = False

    def set_loss_handler(self, callback):
        self.loss_handler = callback

    def directory(self, ticket):
        return self.state/unit_for(ticket)

    def identity(self, ticket):
        unit = unit_for(ticket)
        meta = self.manager.describe(unit)
        ready = message(self.directory(ticket), 'ready')
        path = meta['ControlGroup'] + '/payload'
        if ready['invocation_id'] != meta['InvocationID'] or ready['cgroup_path'] != path:
            raise Refused('LINUX_INVOCATION_CHANGED')
        root = ProcessIdentity(**ready['root'])
        binding = LinuxBinding(ticket, self.host_id, boot_identity(), unit, ready['invocation_id'],
                               path, ready['device'], ready['inode'], root, (root,))
        binding.validate()
        return binding

    def _open(self, binding):
        if self.closed or self.identity(binding.ticket) != binding:
            raise Refused('LINUX_CGROUP_IDENTITY')
        path = Path('/sys/fs/cgroup') / binding.cgroup_path.removeprefix('/')
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (binding.cgroup_device, binding.cgroup_inode):
            os.close(fd)
            raise Refused('LINUX_CGROUP_IDENTITY')
        return fd

    def handshake(self, owner, generation, bindings):
        targets = {}
        for target in bindings:
            fd = self._open(target.binding)
            os.close(fd)
            targets[target.binding] = target.known_processes
        self.targets = targets
        return ObserverHandshake(owner, generation, self.clock(), tuple(digest(asdict(t)) for t in bindings))

    def read(self, target, epoch, generation):
        binding = target.binding if type(target) is ObservationTarget else target
        required = self.targets.get(binding)
        if not required or (type(target) is ObservationTarget and target.known_processes != required):
            raise Refused('LINUX_OBSERVER_COVERAGE')
        fd = self._open(binding)
        try:
            try:
                sealed = message(self.directory(binding.ticket), 'sealed') == binding.invocation_id
            except FileNotFoundError:
                sealed = False
            populated = group_values(fd)
            observations = []
            for p in dict.fromkeys((*required, *members(fd, binding.cgroup_path))):
                current = process_identity(p.pid)
                if current is not None and current != p:
                    raise Refused('LINUX_REUSED_PROCESS_ID')
                observations.append(ProcessReadback(p, current is None))
            check = self._open(binding)
            os.close(check)
            return LinuxStopReadback(binding, epoch, generation, self.clock(), sealed, populated, tuple(observations))
        finally:
            os.close(fd)

    def close(self, deadline):
        self.closed = True
        self.targets.clear()
