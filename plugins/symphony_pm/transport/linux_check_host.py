"""Trusted host boundary for a separately qualified, contained Linux owner-check.

No host is installed by the plugin. A host implementation must attest its current immutable
runtime, policy, allowlist and OS boundary, mount only the prepared tree read-only, provide
private scratch, disable network/Git/credentials, and independently account for all descendants.
start() is a bounded, one-use dispatch; observe() is query-only and must respect timeout.
"""
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path

from .check_command import CheckCommand, validated_commands
from .linux_host import private_directory
from .linux_sandbox import protected
from .types import Refused, digest, finite, sha


@dataclass(frozen=True)
class LinuxCheckProfile:
    launch_fingerprint: str
    evidence_sha256: str
    root: str
    git: str
    git_sha256: str
    commands: tuple[CheckCommand, ...]
    timeout: float

    @property
    def fingerprint(self):
        return digest(asdict(self))

    def primary_commands(self):
        active = tuple(command for command in self.commands if not command.requires_git)
        if not active:
            raise Refused('CHECK_NO_COMMANDS_EXECUTED')
        return active

    def validate(self):
        if (not all(sha(v, 64) for v in (self.launch_fingerprint, self.evidence_sha256, self.git_sha256))
                or type(self.commands) is not tuple or not finite(self.timeout) or self.timeout <= 0
                or any(not Path(v).is_absolute() for v in (self.root, self.git))):
            raise Refused('CHECK_PROFILE_BINDING')
        validated_commands(self.commands)


@dataclass(frozen=True)
class LinuxCheckInvocation:
    nonce: str
    profile: str
    namespace: str
    job: str
    revision: str
    attempt: str
    request_digest: str
    epoch: int
    source_sha: str
    patch_sha256: str
    tree_oid: str
    directory: str
    directory_id: tuple
    scratch_id: tuple
    command: CheckCommand

    @classmethod
    def from_record(cls, record):
        body = dict(record)
        command = dict(body['command'])
        command['arguments'] = tuple(command['arguments'])
        body.update(command=CheckCommand(**command), directory_id=tuple(body['directory_id']),
                    scratch_id=tuple(body['scratch_id']))
        invocation = cls(**body)
        invocation.validate()
        return invocation

    @property
    def key(self):
        return digest(asdict(self))

    def validate(self):
        if (not sha(self.nonce, 32) or not sha(self.profile, 64) or not sha(self.revision, 64)
                or not sha(self.request_digest, 64) or not sha(self.source_sha, 40)
                or not sha(self.tree_oid, 40) or not sha(self.patch_sha256, 64)
                or not Path(self.directory).is_absolute() or not self.directory_id or not self.scratch_id
                or type(self.epoch) is not int or self.epoch < 1):
            raise Refused('CHECK_INVOCATION_BINDING')
        validated_commands((self.command,))
        if self.command.requires_git:
            raise Refused('CHECK_GIT_TESTS_DEFERRED')


@dataclass(frozen=True)
class LinuxCheckTerminal:
    invocation: str
    profile: str
    epoch: int
    returncode: int
    timed_out: bool
    # Digest of the independent, exact containment stop readback retained by the qualified host.
    stop_receipt_sha256: str

    def matches(self, invocation):
        return (self.invocation == invocation.key and self.profile == invocation.profile
                and type(self.epoch) is int and self.epoch == invocation.epoch
                and type(self.returncode) is int and type(self.timed_out) is bool
                and sha(self.stop_receipt_sha256, 64))


class LinuxCheckHost(ABC):
    @abstractmethod
    def attested_profile(self) -> str:
        """Re-verify the host's own VM qualification and return the pinned profile fingerprint."""

    @abstractmethod
    def start(self, invocation: LinuxCheckInvocation, *, timeout: float) -> None:
        """Dispatch only this one command in its qualified boundary; enforce the hard deadline."""

    @abstractmethod
    def observe(self, invocation: LinuxCheckInvocation, *, timeout: float) -> LinuxCheckTerminal | None:
        """Return fresh stop proof for this invocation, or None. Zero timeout is a nonblocking query."""


@dataclass(frozen=True)
class LinuxOwnerCheck:
    profile: LinuxCheckProfile
    host: LinuxCheckHost

    def verify(self, launcher):
        if launcher.owner_check is not self:
            raise Refused('CHECK_QUALIFICATION_CHANGED')
        if type(self.profile) is not LinuxCheckProfile or not isinstance(self.host, LinuxCheckHost):
            raise Refused('QUALIFIED_CHECKER_REQUIRED')
        self.profile.validate()
        protected(self.profile.git)
        private_directory(self.profile.root)
        launcher.qualification.verify()
        if (self.profile.launch_fingerprint != launcher.qualification.fingerprint
                or self.host.attested_profile() != self.profile.fingerprint):
            raise Refused('CHECK_QUALIFICATION_CHANGED')
