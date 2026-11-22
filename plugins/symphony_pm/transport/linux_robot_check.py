"""Separate qualification for Git-aware Linux project checks on a private candidate export."""
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from pm.filesystem import remove_tree
from .check_command import CheckCommand, DIFF_CHECK
from .linux_check_host import LinuxCheckTerminal
from . import linux_workspace
from .linux_check_config import checked_arguments
from .linux_control import publish, strict_json
from .recapture import directory_identity
from .robot_apply_git import _export_commit
from .types import Refused, digest, sha


@dataclass(frozen=True)
class GitCheckInvocation:
    nonce: str
    profile: str
    epoch: int
    ticket_sha256: str
    source_sha: str
    commit: str
    patch_sha256: str
    directory: str
    directory_id: tuple
    command: CheckCommand

    @classmethod
    def from_record(cls, raw):
        row = dict(raw)
        command = dict(row['command'])
        command['arguments'] = tuple(command['arguments'])
        row.update(directory_id=tuple(row['directory_id']), command=CheckCommand(**command))
        inv = cls(**row)
        inv.validate()
        return inv

    def validate(self):
        if (not all(sha(value, size) for value, size in ((self.nonce, 32), (self.profile, 64),
                (self.ticket_sha256, 64), (self.source_sha, 40), (self.commit, 40), (self.patch_sha256, 64)))
                or type(self.epoch) is not int or self.epoch < 1 or not Path(self.directory).is_absolute()
                or not self.directory_id or self.command.requires_git is not True):
            raise Refused('CHECK_INVOCATION_BINDING')
        checked_arguments(self.command)

    @property
    def key(self):
        return digest(asdict(self))


class LinuxGitCheckHost(ABC):
    """Trusted host with its own qualification. Only the isolated export is exposed to the command."""
    @abstractmethod
    def attested_profile(self):
        """Attest Git, approved tools, read-only export, no network/credentials and precise stop accounting."""

    @abstractmethod
    def start(self, invocation, *, timeout):
        """Enter a bounded invocation once. Return after dispatch; do not wait under the owner lock."""

    @abstractmethod
    def observe(self, invocation, *, timeout):
        """Return a LinuxCheckTerminal for this invocation after its containment has drained."""


@dataclass(frozen=True)
class LinuxGitChecks:
    evidence_sha256: str
    host: LinuxGitCheckHost

    def verify(self, launcher):
        if (launcher.git_checks is not self or not sha(self.evidence_sha256, 64)
                or not isinstance(self.host, LinuxGitCheckHost)):
            raise Refused('LINUX_GIT_CHECK_NOT_QUALIFIED')
        profile = launcher.owner_check.profile
        for command in profile.commands:
            if command.requires_git and command != DIFF_CHECK:
                checked_arguments(command)
        pin = digest(dict(kind='linux-git-check-v1', primary_profile=profile.fingerprint,
                          evidence_sha256=self.evidence_sha256))
        if self.host.attested_profile() != pin:
            raise Refused('LINUX_GIT_CHECK_QUALIFICATION_CHANGED')
        return pin

    def run(self, launcher, pin, record, repository, command, timeout, fence, starting, settle):
        profile = launcher.owner_check.profile
        if self.verify(launcher) != pin:
            raise Refused('LINUX_GIT_CHECK_QUALIFICATION_CHANGED')
        if command not in profile.commands or not command.requires_git or command == DIFF_CHECK:
            raise Refused('CHECK_COMMAND_POLICY')
        root = Path(launcher.settings.state)/'robot-git-check.active'
        nonce = uuid4().hex
        parent = getattr(self.host, 'export_root', None)
        export = parent/('g-' + nonce) if parent is not None else root
        root.mkdir(mode=0o700, exist_ok=False)
        release = True
        owned_export = root
        try:
            if export != root:
                export.mkdir(mode=0o700)
                owned_export = export
                publish(root, 'export-id.json', directory_identity(export))
            identity, commit = record['identity'], record['commit']
            source = identity['source_sha']
            clone = _export_commit(profile.git, repository, source, commit, export)
            invocation = GitCheckInvocation(nonce, pin, record['owner_epoch'], identity['ticket_sha256'],
                                            source, commit, identity['patch_sha256'], str(clone),
                                            tuple(directory_identity(clone)), command)
            publish(root, 'invocation.json', asdict(invocation))
            linux_workspace._sync_directory(root.parent)
            with fence():
                if self.verify(launcher) != pin:
                    raise Refused('LINUX_GIT_CHECK_QUALIFICATION_CHANGED')
                starting(invocation.key)
                release = False
                self.host.start(invocation, timeout=timeout)
            result = self.host.observe(invocation, timeout=timeout)
            if self.verify(launcher) != pin:
                raise Refused('LINUX_GIT_CHECK_QUALIFICATION_CHANGED')
            if type(result) is not LinuxCheckTerminal or not result.matches(invocation):
                raise Refused('CHECK_STOP_UNPROVEN')
            with fence():
                settle(invocation, result)
                release = True
                if result.timed_out or result.returncode not in (0, 1):
                    raise Refused('ROBOT_CHECK_INCOMPLETE')
                return project_result(invocation, result)
        finally:
            # An ambiguous dispatch or readback retains the export and fences later work.
            if release:
                retire(root, owned_export)


def project_result(invocation, terminal):
    return dict(name=invocation.command.name, source_sha=invocation.source_sha, candidate_sha=invocation.commit,
                exit_code=terminal.returncode, invocation=invocation.key, profile=invocation.profile,
                stop_receipt_sha256=terminal.stop_receipt_sha256)


def settled_record(record, inv, result):
    identity = record['identity']
    if (record.get('git_invocation') != inv.key or record['owner_epoch'] != inv.epoch
            or (identity['ticket_sha256'], identity['source_sha'], identity['patch_sha256'], record['commit'])
            != (inv.ticket_sha256, inv.source_sha, inv.patch_sha256, inv.commit)
            or not result.matches(inv)):
        raise Refused('CHECK_INTENT_CHANGED')
    terminal = asdict(result)
    if 'terminal' in record:
        if record['terminal'] != terminal:
            raise Refused('CHECK_STOP_CONFLICT')
        return record
    settled = dict(record, state='CHECK_ABORTED', terminal=terminal)
    if not result.timed_out and result.returncode in (0, 1):
        settled.update(state='CHECK_RECOVERED', git_checks=[*record['git_checks'], project_result(inv, result)])
    return settled


def retire(root, export):
    if root != export and export.exists():
        remove_tree(export)
        linux_workspace._sync_directory(export.parent)
    # Atomically free the slot only after persisting the terminal receipt. A crash
    # during recursive cleanup must not leave a half-deleted active marker.
    retired = root.with_name('git-check-done-' + uuid4().hex)
    root.rename(retired)
    linux_workspace._sync_directory(root.parent)
    remove_tree(retired)


def reconcile_git_check(launcher, owner):
    checker = launcher.git_checks
    root = Path(launcher.settings.state)/'robot-git-check.active'
    if checker is None or not root.exists() or not launcher.git_lane.acquire(blocking=False):
        return
    try:
        inv = GitCheckInvocation.from_record(strict_json(root/'invocation.json'))
        if checker.verify(launcher) != inv.profile:
            return
        parent = getattr(checker.host, 'export_root', None)
        export = parent/('g-' + inv.nonce) if parent is not None else root
        src = export/'src'
        if export != root and export.exists() and (
                export.resolve(strict=True) != export or directory_identity(export) != strict_json(root/'export-id.json')):
            return
        if Path(inv.directory) != src or (src.exists() and tuple(directory_identity(src)) != inv.directory_id):
            return
        result = checker.host.observe(inv, timeout=0)
        if type(result) is not LinuxCheckTerminal or not result.matches(inv) or checker.verify(launcher) != inv.profile:
            return
        owner.guard.assert_owned()
        owner.journal.settle_git_check(owner.token, inv, result)
        retire(root, export)
    except (OSError, Refused, ValueError, TypeError, KeyError, TimeoutError):
        # Missing, conflicting or inaccessible evidence keeps the slot held.
        return
    finally:
        launcher.git_lane.release()
