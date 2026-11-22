"""Recover the patch of a stopped attempt using only its saved tree and private baseline evidence."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import stat
import tempfile

from hermes_platform.resolver import locate_command
from . import attempt_workspace as ws
from .types import Refused, digest, sha


@dataclass(frozen=True)
class LegacyArchive:
    """Owner attestation for one verified copy of a pre-v2 archive without a directory receipt."""
    ticket_digest: str
    source_digest: str
    baseline_digest: str
    directory_id: tuple


def attempt_data(qualification, ticket):
    return (Path(qualification.profile_home)/'plugin-data'/'symphony_pm'/digest(ticket.namespace)[:32]
            / 'attempts'/digest(ticket.attempt)[:12])


def archive_path(root, attempt):
    if type(attempt) is not str or not attempt.startswith('attempt-') or not sha(attempt[8:], 32):
        raise Refused('ATTEMPT_WORKSPACE_IDENTITY')
    return Path(root)/attempt


def directory_identity(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise Refused('ATTEMPT_WORKSPACE_IDENTITY')
    if not info.st_ino:
        raise Refused('ATTEMPT_WORKSPACE_IDENTITY')
    return [info.st_dev, info.st_ino, getattr(info, 'st_birthtime_ns', None)]


def _verify_archive(row, data, workspace, saved_index, legacy):
    actual = directory_identity(workspace.path)
    if row['owner_identity'] is not None:
        try:
            recorded = json.loads((data/'workspace-id.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            raise Refused('ATTEMPT_WORKSPACE_IDENTITY') from exc
        if actual != recorded:
            raise Refused('ATTEMPT_WORKSPACE_IDENTITY')
        return
    if (type(legacy) is not LegacyArchive or tuple(actual) != legacy.directory_id
            or legacy.ticket_digest != digest(json.loads(row['ticket'])) or not sha(legacy.source_digest, 64)
            or (row['proof_source'] or '').rpartition(':')[2] != legacy.source_digest):
        raise Refused('LEGACY_ARCHIVE_UNVERIFIED')
    with saved_index.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != legacy.baseline_digest:
            raise Refused('ATTEMPT_BASELINE_MISMATCH')


def read_bound_patch(directory, patch):
    from .attempt_workspace import PATCH_LIMIT
    if not isinstance(patch.get('path'), str):
        raise Refused("ROBOT_PATCH_LOCATION")
    path = Path(patch['path'])
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_relative_to(directory):
            raise Refused("ROBOT_PATCH_LOCATION")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refused("ROBOT_PATCH_LOCATION")
        with path.open('rb') as stream:
            content = stream.read(PATCH_LIMIT + 1)
    except OSError as exc:
        raise Refused("ROBOT_PATCH_UNAVAILABLE") from exc
    if len(content) > PATCH_LIMIT or hashlib.sha256(content).hexdigest() != patch['sha256']:
        raise Refused("ROBOT_PATCH_DIGEST")
    return content


def recapture(launcher, journal, token, ticket, row):
    """The owner lock and a fenced append bind each immutable patch to its observation sequence."""
    if ticket.namespace != launcher.qualification.namespace or token.profile_home != launcher.qualification.profile_home:
        raise Refused('LAUNCHER_PROFILE_BINDING')
    if row['state'] != 'STOPPED':
        raise Refused('RECAPTURE_STOP_REQUIRED')
    seq = journal.next_observation_seq(token, ticket)
    git = locate_command('git').command
    if not git:
        raise Refused('OWNER_GIT_UNAVAILABLE')
    settings = launcher.settings
    workspace = ws.Workspace(ticket.attempt, archive_path(settings.attempts_root, ticket.attempt))
    data = attempt_data(launcher.qualification, ticket)
    saved_index = data/'shadow'/'baseline.index'
    if not saved_index.is_file():
        raise Refused('ATTEMPT_BASELINE_MISSING')
    _verify_archive(row, data, workspace, saved_index, settings.legacy_archive)
    directory_identity(workspace.src)
    # A crash between writing the files and appending the row leaves a private orphan.
    # A later call may safely use the same sequence with a fresh output directory.
    output = Path(tempfile.mkdtemp(prefix='o-', dir=data))
    capture = ws.Capture(git[0], settings.repository, ticket.plan.source_sha, workspace, output/'s', output/'r')
    capture.restore_baseline(saved_index)
    result = capture.result(observation_seq=seq)
    _verify_archive(row, data, workspace, saved_index, settings.legacy_archive)
    observation = {'kind': 'CAPTURE', 'source_sha': ticket.plan.source_sha, 'base_tree': capture.base,
                   'baseline_matches_source': result.baseline_matches_source,
                   'patch': {'path': str(result.patch), 'sha256': result.sha256, 'paths': list(result.paths)}}
    journal.append_observation(token, ticket, observation, expected_seq=seq)
    return {'seq': seq, **observation}
