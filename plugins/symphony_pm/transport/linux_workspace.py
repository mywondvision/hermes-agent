"""Pinned source export and owner-side capture of a quiescent Linux workspace."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from hermes_platform.resolver import locate_command
from . import attempt_workspace as ws
from .linux_control import strict_json
from .recapture import archive_path, attempt_data, directory_identity
from .types import Refused


def _git():
    resolved = locate_command('git').command
    if not resolved:
        raise Refused('OWNER_GIT_UNAVAILABLE')
    return resolved[0]


def _mountpoints():
    def decode(raw):
        return os.fsdecode(re.sub(rb'\\([0-7]{3})', lambda m: bytes([int(m[1], 8)]), raw))
    with open('/proc/self/mountinfo', 'rb') as stream:
        return {Path(decode(line.split()[4])) for line in stream}


def _source_modes(shadow, src):
    modes = {}
    for entry in shadow.run(src, 'base.index', 'ls-files', '--stage', '-z').split(b'\0'):
        if not entry:
            continue
        meta, raw_name = entry.split(b'\t', 1)
        mode, _, stage = meta.split()
        name = raw_name.decode('utf-8')
        if mode not in (b'100644', b'100755') or stage != b'0':
            raise Refused('LINUX_SOURCE_FILE_TYPE')
        modes[name] = mode == b'100755'
    return modes


def _validate_tree(root, modes, mounts):
    """Run after the controller has sealed and drained the payload, before Git reads files."""
    if root.resolve(strict=True) != root or any(m in (root, root.parent) or root in m.parents for m in mounts):
        raise Refused('LINUX_WORKSPACE_MOUNT')
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        raise Refused('LINUX_WORKSPACE_TYPE')
    total_bytes = 0
    count = 0
    pending = [root]
    while pending:
        current = pending.pop()
        for path in current.iterdir():
            info = path.lstat()
            count += 1
            if stat.S_ISREG(info.st_mode):
                total_bytes += info.st_size
            if count > 100000 or total_bytes > 512 * 1024 * 1024:
                raise Refused('LINUX_CAPTURE_LIMIT')
            if (info.st_dev != root_info.st_dev or path.name.casefold() == '.git'
                    or info.st_mode & (stat.S_ISUID | stat.S_ISGID)
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise Refused('LINUX_WORKSPACE_POLICY')
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Refused('LINUX_WORKSPACE_TYPE')
            elif bool(info.st_mode & 0o111) != modes.get(path.relative_to(root).as_posix(), False):
                raise Refused('LINUX_WORKSPACE_MODE')


def _tree_digest(root):
    """Hash only a bounded, validated, stopped tree. Include names, modes, and file data."""
    result = hashlib.sha256()
    for p in sorted(root.rglob('*')):
        info = p.lstat()
        name = p.relative_to(root).as_posix().encode('utf-8')
        result.update(len(name).to_bytes(8, 'big') + name + info.st_mode.to_bytes(4, 'big'))
        if stat.S_ISREG(info.st_mode):
            with p.open('rb') as stream:
                result.update(hashlib.file_digest(stream, 'sha256').digest())
    return result.hexdigest()


def seal_workspace(launcher, ticket, archive):
    data = attempt_data(launcher.qualification, ticket)
    receipt = data/'sealed-tree.sha256'
    workspace = ws.Workspace(ticket.attempt, Path(archive))
    if strict_json(data/'workspace-id.json') != directory_identity(workspace.path):
        raise Refused('ATTEMPT_WORKSPACE_IDENTITY')
    modes = strict_json(data/'source-modes.json', limit=None)
    _validate_tree(workspace.src, modes, _mountpoints())
    measured = _tree_digest(workspace.src)
    try:
        with receipt.open('x', encoding='ascii') as stream:
            stream.write(measured)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if receipt.read_text(encoding='ascii') != measured:
            raise Refused('LINUX_ARCHIVE_CHANGED')
    # The archive and its receipt must both survive a writer crash before hold release.
    for parent in (workspace.path.parent, data):
        _sync_directory(parent)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LinuxCapture(ws.Capture):
    def validate(self):
        _validate_tree(self.workspace.src, _source_modes(self.shadow, self.workspace.src), _mountpoints())


def prepare(launcher, ticket):
    workspace = ws.Workspace(ticket.attempt, Path(launcher.settings.active))
    data = attempt_data(launcher.qualification, ticket)
    data.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipt = directory_identity(workspace.path)
    for directory in (workspace.src, workspace.home, workspace.temp):
        directory.mkdir(mode=0o700)
    capture = LinuxCapture(_git(), launcher.settings.repository, ticket.plan.source_sha, workspace,
                           data/'shadow', data/'results')
    modes = _source_modes(capture.shadow, workspace.src)
    (data/'source-modes.json').write_text(json.dumps(modes), encoding='utf-8')
    capture.shadow.run(workspace.src, 'base.index', 'checkout-index', '-a')
    capture.validate()
    capture.baseline()
    (data/'workspace-id.json').write_text(json.dumps(receipt), encoding='utf-8')
    capture.receipt = data/'sealed-tree.sha256'
    return data, workspace, capture


def capture_patch(capture, *, seq=None):
    capture.validate()
    receipt = capture.receipt
    expected = receipt.read_text(encoding='ascii')
    if _tree_digest(capture.workspace.src) != expected:
        raise Refused('LINUX_ARCHIVE_CHANGED')
    result = capture.result(observation_seq=seq)
    if _tree_digest(capture.workspace.src) != expected:
        raise Refused('LINUX_ARCHIVE_CHANGED')
    return {'path': str(result.patch), 'sha256': result.sha256, 'paths': list(result.paths)}


def recapture(launcher, journal, token, ticket, row):
    if (ticket.namespace != launcher.qualification.namespace or token.profile_home != launcher.qualification.profile_home
            or not journal.proven_stop(row) or row['hold_reason']):
        raise Refused('RECAPTURE_REQUIRES_PROVEN_STOP')
    workspace = ws.Workspace(ticket.attempt, archive_path(launcher.settings.attempts_root, ticket.attempt))
    data = attempt_data(launcher.qualification, ticket)
    identity = strict_json(data/'workspace-id.json')
    if directory_identity(workspace.path) != identity:
        raise Refused('ATTEMPT_WORKSPACE_IDENTITY')
    seq = journal.next_observation_seq(token, ticket)
    output = Path(tempfile.mkdtemp(prefix='o-', dir=data))
    capture = LinuxCapture(_git(), launcher.settings.repository, ticket.plan.source_sha, workspace,
                           output/'shadow', output/'results')
    capture.receipt = data/'sealed-tree.sha256'
    capture.restore_baseline(data/'shadow'/'baseline.index')
    patch = capture_patch(capture, seq=seq)
    if directory_identity(workspace.path) != identity:
        raise Refused('ATTEMPT_WORKSPACE_IDENTITY')
    observation = {'kind': 'CAPTURE', 'source_sha': ticket.plan.source_sha, 'base_tree': capture.base,
                   'baseline_matches_source': True, 'patch': patch}
    journal.append_observation(token, ticket, observation, expected_seq=seq)
    return {'seq': seq, **observation}
