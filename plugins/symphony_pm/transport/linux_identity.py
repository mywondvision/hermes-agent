"""Versioned identities persisted independently of transient cgroup and process handles."""
from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from .types import ExecutionPlan, LaunchTicket, ProcessIdentity, Refused, digest, sha

CONTRACT = 'linux-systemd-cgroup-v2/1'


def unit_for(ticket):
    return 'hermes-symphony@' + digest([ticket.namespace, ticket.project, ticket.job,
                                        ticket.revision, ticket.attempt]) + '.service'


@dataclass(frozen=True)
class LinuxBinding:
    ticket: LaunchTicket
    host_id: str
    boot_id: str
    unit: str
    invocation_id: str
    cgroup_path: str
    cgroup_device: int
    cgroup_inode: int
    root: ProcessIdentity
    known_processes: tuple
    backend: str = CONTRACT

    @property
    def effect_key(self):
        t = self.ticket
        return digest([t.namespace, t.project, t.job, t.revision, t.attempt, 'cancel'])

    def validate(self):
        path = PurePosixPath(self.cgroup_path)
        if (self.backend != CONTRACT or not sha(self.host_id, 64) or not sha(self.boot_id, 32)
                or self.unit != unit_for(self.ticket) or not sha(self.invocation_id, 32)
                or not path.is_absolute() or self.cgroup_path != str(path) or '..' in path.parts
                or path.parts[-2:] != (self.unit, 'payload')
                or type(self.cgroup_device) is not int or self.cgroup_device <= 0
                or type(self.cgroup_inode) is not int or self.cgroup_inode <= 0
                or self.root not in self.known_processes or not all(
                    type(p) is ProcessIdentity and p.valid() and re.fullmatch(r'[0-9]+', p.creation)
                    for p in self.known_processes)
                or len({p.pid for p in self.known_processes}) != len(self.known_processes)):
            raise Refused('LINUX_NATIVE_BINDING')


def linux_binding_from_json(value):
    try:
        if type(value) is not dict or value.get('backend') != CONTRACT:
            raise Refused('LINUX_NATIVE_BINDING')
        body = dict(value)
        ticket = dict(body['ticket'])
        ticket['plan'] = ExecutionPlan(**ticket['plan'])
        body.update(ticket=LaunchTicket(**ticket), root=ProcessIdentity(**body['root']),
                    known_processes=tuple(ProcessIdentity(**item) for item in body['known_processes']))
        binding = LinuxBinding(**body)
        binding.validate()
        return binding
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Refused('LINUX_NATIVE_BINDING') from exc


@dataclass(frozen=True)
class LinuxStopReadback:
    binding: LinuxBinding
    epoch: int
    generation: int
    observed_at: float
    launch_closed: bool
    populated: int
    processes: tuple
    source: str = 'linux-cgroup.events/1'
