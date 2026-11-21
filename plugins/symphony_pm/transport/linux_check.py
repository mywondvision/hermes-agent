"""Check a primary capture in a disposable export; never execute a check in the owner process."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

from pm.filesystem import remove_tree
from . import attempt_workspace as ws
from .linux_check_host import LinuxCheckInvocation, LinuxCheckTerminal, LinuxOwnerCheck
from .linux_control import publish, strict_json
from . import linux_workspace
from .recapture import archive_path, attempt_data, directory_identity
from .types import Refused, digest, finite, sha


def check_marker(launcher):
    return Path(launcher.settings.state)/'owner-check.active'


def _read_invocation(marker):
    record = strict_json(marker, limit=None)
    invocation = LinuxCheckInvocation.from_record(record['invocation'])
    if record['key'] != invocation.key:
        raise Refused('CHECK_INTENT_CHANGED')
    return invocation


@contextmanager
def _fence(owner, token, *, dispatch):
    # As with native create/cancel, writer rotation must not interleave with host entry.
    with owner.lock:
        if dispatch:
            owner._check(token)
        else:
            owner.guard.assert_owned()
        with owner.journal._transaction(token):
            yield


def _acknowledge(marker, data, invocation, terminal, *, cleanup, verify_tree=None):
    if type(terminal) is not LinuxCheckTerminal or not terminal.matches(invocation):
        raise Refused('CHECK_STOP_UNPROVEN')
    if _read_invocation(marker) != invocation:
        raise Refused('CHECK_INTENT_CHANGED')
    receipt = data/('check-stop-' + invocation.nonce + '.json')
    try:
        publish(receipt.parent, receipt.name, asdict(terminal))
    except FileExistsError:
        if strict_json(receipt) != asdict(terminal):
            raise Refused('CHECK_STOP_CONFLICT')
    try:
        if verify_tree is not None:
            verify_tree()
    finally:
        # A successor must find the intent until this exact, stopped invocation's copy is gone.
        cleanup()
        marker.unlink()
        linux_workspace._sync_directory(marker.parent)


def reconcile_check(launcher, owner):
    """Read an uncertain invocation once; a late terminal never changes the primary CHECK."""
    checker, marker = launcher.owner_check, check_marker(launcher)
    if type(checker) is not LinuxOwnerCheck:
        return
    try:
        invocation = _read_invocation(marker)
        if owner._submitting[invocation.request_digest] or invocation.profile != checker.profile.fingerprint:
            return
        # The marker is written by this profile; verify its persisted source ticket independently.
        # Project is fixed by the Linux profile and cannot be supplied by the marker.
        project = launcher.settings.project_ref
        ticket, _ = owner.journal.read_launch(owner.token, project, invocation.job, invocation.revision)
        if (ticket.namespace, ticket.attempt, ticket.request_digest, ticket.plan.source_sha) != (
                invocation.namespace, invocation.attempt, invocation.request_digest, invocation.source_sha):
            return
        data = attempt_data(launcher.qualification, ticket)
        receipt = data/('check-stop-' + invocation.nonce + '.json')
        try:
            info = receipt.lstat()
        except FileNotFoundError:
            checker.verify(launcher)
            terminal = checker.host.observe(invocation, timeout=0)
            checker.verify(launcher)
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or receipt.resolve(strict=True) != receipt:
                return
            terminal = LinuxCheckTerminal(**strict_json(receipt))
        with _fence(owner, owner.token, dispatch=False):
            cleanup = lambda: _dispose(Path(invocation.directory).parent, checker.profile, invocation.scratch_id)
            _acknowledge(marker, data, invocation, terminal, cleanup=cleanup)
    except Exception:  # health: allow BLE001 -- loss of query evidence must preserve the hold on restart
        return


def _read_capture(data, ticket, capture, patch):
    expected = data/'results'/(ticket.attempt+'.patch')
    receipt = expected.with_suffix('.json')
    if (Path(patch['path']) != expected or capture.sha != ticket.plan.source_sha or capture.base != capture.source_tree
            or any(p.resolve(strict=True) != p or not stat.S_ISREG(p.lstat().st_mode) or p.stat().st_nlink != 1
                   for p in (expected, receipt)) or expected.stat().st_size > ws.PATCH_LIMIT):
        raise Refused('CHECK_CAPTURE_BINDING')
    body = strict_json(receipt, limit=None)
    raw = expected.read_bytes()
    if (not raw or hashlib.sha256(raw).hexdigest() != patch['sha256'] or body['sha256'] != patch['sha256']
            or body['attempt'] != ticket.attempt or body['source_sha'] != ticket.plan.source_sha
            or body['paths'] != patch['paths'] or body['baseline_matches_source'] is not True
            or body['base_tree'] != capture.base):
        raise Refused('CHECK_PATCH_CHANGED')
    ws.require_patch_policy(raw, patch['paths'])
    return raw, body


@dataclass(frozen=True)
class _Prepared:
    src: Path
    identity: tuple
    tree: str
    scratch_id: tuple
    modes: dict
    blobs: dict

    def verify(self):
        if tuple(directory_identity(self.src)) != self.identity:
            raise Refused('CHECK_COPY_CHANGED')
        linux_workspace._validate_tree(self.src, self.modes, linux_workspace._mountpoints())
        files = {}
        directories = {self.src}
        for path in self.src.rglob('*'):
            if path.is_dir():
                continue
            size = path.stat().st_size
            blob = hashlib.sha1(b'blob ' + str(size).encode('ascii') + b'\0')
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b''):
                    blob.update(chunk)
            files[path.relative_to(self.src).as_posix()] = blob.hexdigest()
            directories.update(parent for parent in path.parents if parent == self.src or self.src in parent.parents)
        if files != self.blobs or any(p not in directories for p in self.src.rglob('*') if p.is_dir()):
            raise Refused('CHECK_COPY_CHANGED')


def _dispose(directory, profile, identity):
    root = Path(profile.root)
    try:
        directory.lstat()
    except FileNotFoundError:
        # Deletion may have finished just before the previous writer could fsync the parent.
        if directory.parent != root:
            raise Refused('CHECK_SCRATCH_CHANGED')
        linux_workspace._sync_directory(root)
        return
    if (directory.parent != root or not directory.name.startswith('c-') or directory.resolve(strict=True) != directory
            or tuple(directory_identity(directory)) != identity or any(m == directory or directory in m.parents
                                                                       for m in linux_workspace._mountpoints())):
        raise Refused('CHECK_SCRATCH_CHANGED')
    remove_tree(directory)
    linux_workspace._sync_directory(root)


@contextmanager
def _prepared_tree(launcher, checker, ticket, capture, patch, data):
    profile = checker.profile
    root = Path(profile.root)
    outside = (launcher.settings.repository, launcher.settings.attempts_root, launcher.settings.state,
               launcher.qualification.profile_home)
    if (not root.is_dir() or root.resolve(strict=True) != root or any(root == Path(p).resolve()
            or root in Path(p).resolve().parents or Path(p).resolve() in root.parents for p in outside)):
        raise Refused('CHECK_ROOT_BINDING')
    git = Path(profile.git)
    if Path(capture.git).resolve(strict=True) != git or git.resolve(strict=True) != git:
        raise Refused('CHECK_GIT_CHANGED')
    with git.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != profile.git_sha256:
            raise Refused('CHECK_GIT_CHANGED')
    raw, body = _read_capture(data, ticket, capture, patch)
    directory = Path(tempfile.mkdtemp(prefix='c-', dir=root))
    scratch_id = tuple(directory_identity(directory))
    try:
        yield _export(directory, scratch_id, launcher, ticket, git, raw, body, patch)
    finally:
        try:
            check_marker(launcher).lstat()
        except FileNotFoundError:
            _dispose(directory, profile, scratch_id)


def _export(directory, scratch_id, launcher, ticket, git, raw, body, patch):
    src = directory/'src'
    src.mkdir()
    shadow = ws.Shadow(str(git), directory/'s', launcher.settings.repository)
    index = 'base.index'
    shadow.run(src, index, 'read-tree', ticket.plan.source_sha)
    if (shadow.run(src, index, 'rev-parse', ticket.plan.source_sha+'^{commit}').decode().strip() != ticket.plan.source_sha
            or shadow.run(src, index, 'write-tree').decode().strip() != body['base_tree']):
        raise Refused('CHECK_SOURCE_CHANGED')
    linux_workspace._source_modes(shadow, src)
    fixed_patch = directory/'input.patch'
    fixed_patch.write_bytes(raw)
    # Apply to the new private index. Checkout materializes only the verified, patched tree.
    shadow.run(src, index, 'apply', '--cached', '--binary', str(fixed_patch))
    after = shadow.run(src, index, 'write-tree').decode('ascii').strip()
    if after != body['after_tree']:
        raise Refused('CHECK_PATCH_TREE_CHANGED')
    names = shadow.run(src, index, 'diff', '--name-only', '-z', '--no-renames', body['base_tree'], after)
    if tuple(sorted(os.fsdecode(v) for v in names.split(b'\0') if v)) != tuple(patch['paths']):
        raise Refused('CHECK_PATCH_PATHS_CHANGED')
    modes = linux_workspace._source_modes(shadow, src)
    shadow.run(src, index, 'checkout-index', '-a')
    blobs = {}
    for entry in shadow.run(src, index, 'ls-files', '--stage', '-z').split(b'\0'):
        if entry:
            meta, path = entry.split(b'\t', 1)
            blobs[os.fsdecode(path)] = meta.split()[1].decode('ascii')
    return _Prepared(src, tuple(directory_identity(src)), after, scratch_id, modes, blobs)


def _command(launcher, checker, ticket, runner, patch, prepared, command, data):
    checker.verify(launcher)
    profile = checker.profile
    invocation = LinuxCheckInvocation(uuid.uuid4().hex, profile.fingerprint, ticket.namespace, ticket.job,
        ticket.revision, ticket.attempt, ticket.request_digest, runner.token.epoch, ticket.plan.source_sha,
        patch['sha256'], prepared.tree, str(prepared.src), prepared.identity, prepared.scratch_id, command)
    invocation.validate()
    marker = check_marker(launcher)
    started = time.monotonic()
    deadline = started + profile.timeout
    with _fence(runner.owner, runner.token, dispatch=True):
        checker.verify(launcher)
        prepared.verify()
        publish(marker.parent, marker.name, {'key': invocation.key, 'invocation': asdict(invocation)})
        checker.host.start(invocation, timeout=profile.timeout)
    timeout = max(0, deadline-time.monotonic())
    terminal = checker.host.observe(invocation, timeout=timeout)
    checker.verify(launcher)
    with _fence(runner.owner, runner.token, dispatch=False):
        cleanup = lambda: _dispose(prepared.src.parent, profile, prepared.scratch_id)
        _acknowledge(marker, data, invocation, terminal, cleanup=cleanup, verify_tree=prepared.verify)
        finished = time.monotonic()
        if terminal.timed_out or finished > deadline:
            raise TimeoutError('CHECK_TIMEOUT')
        # Retain the exact spent invocation after its active marker and disposable tree are removed.
        record = {'invocation': asdict(invocation), 'ticket': digest(asdict(ticket)), 'capture': digest(patch),
                  'terminal': digest(asdict(terminal)), 'started': started, 'finished': finished}
        publish(data, 'check-run-' + invocation.nonce + '.json', record)
    return terminal, {'nonce': invocation.nonce, 'sha256': digest(record)}


def check_patch(launcher, ticket, runner, capture, patch, *, completed):
    outcome = {'attempt': ticket.attempt, 'source_sha': ticket.plan.source_sha, 'patch_sha256': patch['sha256'],
               'ok': False, 'apply': None, 'commands': {}, 'deferred': []}
    try:
        if not completed:
            raise Refused('CHECK_ATTEMPT_INTERRUPTED')
        checker = launcher.owner_check
        if type(checker) is not LinuxOwnerCheck:
            raise Refused('QUALIFIED_CHECKER_REQUIRED')
        checker.verify(launcher)
        commands = checker.profile.commands
        outcome['revisions'] = {'profile': checker.profile.fingerprint, 'evidence': checker.profile.evidence_sha256}
        outcome['deferred'] = [c.name for c in commands if c.requires_git]
        active = [c for c in commands if not c.requires_git]
        if not active:
            raise Refused('CHECK_NO_COMMANDS_EXECUTED')
        data = attempt_data(launcher.qualification, ticket)
        outcome['stop_receipts'] = {}
        outcome['runs'] = {}
        for command in active:
            # Each immutable copy belongs to one invocation and one durable terminal receipt.
            with _prepared_tree(launcher, checker, ticket, capture, patch, data) as prepared:
                outcome['apply'] = 0
                stop, outcome['runs'][command.name] = _command(launcher, checker, ticket, runner, patch, prepared, command, data)
                outcome['commands'][command.name] = stop.returncode
                outcome['stop_receipts'][command.name] = stop.stop_receipt_sha256
                if stop.returncode:
                    raise Refused('CHECK_COMMAND_FAILED')
        checker.verify(launcher)
        if (capture.workspace.path != archive_path(launcher.settings.attempts_root, ticket.attempt)
                or capture.receipt != data/'sealed-tree.sha256'):
            raise Refused('CHECK_CAPTURE_BINDING')
        capture.validate()
        if linux_workspace._tree_digest(capture.workspace.src) != capture.receipt.read_text(encoding='ascii'):
            raise Refused('LINUX_ARCHIVE_CHANGED')
        _, receipt = _read_capture(data, ticket, capture, patch)
        if receipt['after_tree'] != prepared.tree:
            raise Refused('CHECK_PATCH_TREE_CHANGED')
        if outcome['deferred']:
            raise Refused('CHECK_GIT_TESTS_DEFERRED')
        outcome['ok'] = True
    except Refused as exc:
        outcome['reason'] = str(exc)
    except TimeoutError:
        outcome['reason'] = 'CHECK_TIMEOUT'
    except Exception as exc:  # health: allow BLE001 -- primary capture survives every checking failure
        outcome['reason'] = 'CHECK_FAILED_' + type(exc).__name__
    try:
        check_marker(launcher).lstat()
    except FileNotFoundError:
        pass
    except OSError:
        outcome['execution_hold'] = True
    else:
        outcome['execution_hold'] = True
    return outcome


def _saved_command(data, profile, ticket, epoch, patch, tree, command, check):
    reference = check['runs'][command.name]
    nonce = reference['nonce']
    if not sha(nonce, 32) or not sha(reference['sha256'], 64):
        return False
    run_path = data/('check-run-' + nonce + '.json')
    stop_path = data/('check-stop-' + nonce + '.json')
    if any(p.lstat().st_nlink != 1 for p in (run_path, stop_path)):
        return False
    run, stop = strict_json(run_path), strict_json(stop_path)
    invocation = LinuxCheckInvocation.from_record(run['invocation'])
    terminal = LinuxCheckTerminal(**stop)
    return (invocation.tree_oid == tree and digest(run) == reference['sha256'] and run['ticket'] == digest(asdict(ticket))
            and run['capture'] == digest(patch) and run['terminal'] == digest(stop)
            and (invocation.nonce, invocation.profile, invocation.command) == (nonce, profile.fingerprint, command)
            and (invocation.namespace, invocation.job, invocation.revision, invocation.attempt, invocation.request_digest,
                 invocation.epoch, invocation.source_sha, invocation.patch_sha256)
                == (ticket.namespace, ticket.job, ticket.revision, ticket.attempt, ticket.request_digest,
                    epoch, ticket.plan.source_sha, patch['sha256'])
            and terminal.matches(invocation) and terminal.timed_out is False
            and check['stop_receipts'][command.name] == terminal.stop_receipt_sha256
            and type(check['commands'][command.name]) is int and check['commands'][command.name] == terminal.returncode
            and finite(run['started']) and finite(run['finished']) and 0 <= run['finished']-run['started'] <= profile.timeout)


def saved_commands(launcher, ticket, epoch, patch, check):
    profile = launcher.owner_check.profile
    data = attempt_data(launcher.qualification, ticket)
    commands = tuple(c for c in profile.commands if not c.requires_git)
    names = {c.name for c in commands}
    try:
        if not all(type(check.get(k)) is dict and set(check[k]) == names for k in ('commands', 'stop_receipts', 'runs')):
            return False
        patch_path = data/'results'/(ticket.attempt + '.patch')
        metadata = patch_path.with_suffix('.json')
        if (Path(patch['path']) != patch_path or metadata.lstat().st_nlink != 1
                or patch_path.resolve(strict=True) != patch_path or patch_path.lstat().st_nlink != 1
                or not stat.S_ISREG(patch_path.lstat().st_mode) or patch_path.stat().st_size > ws.PATCH_LIMIT):
            return False
        receipt = strict_json(metadata, limit=None)
        if ((receipt['source_sha'], receipt['attempt'], receipt['sha256'], receipt['paths'])
                != (ticket.plan.source_sha, ticket.attempt, patch['sha256'], patch['paths'])
                or receipt['baseline_matches_source'] is not True or not sha(receipt['after_tree'], 40)):
            return False
        with patch_path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != patch['sha256']:
                return False
        return all(_saved_command(data, profile, ticket, epoch, patch, receipt['after_tree'], c, check)
                   for c in commands)
    except (AttributeError, KeyError, TypeError, ValueError, Refused, OSError):
        return False
