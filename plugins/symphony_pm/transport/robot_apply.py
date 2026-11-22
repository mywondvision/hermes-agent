"""Owner-authorized Robot apply, with a durable intent before Git creates the local branch."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess

from . import robot_apply_git as git_ops
from .attempt_workspace import PATCH_LIMIT
from hermes_platform.resolver import locate_command
from .owner_check_profile import DIFF_CHECK, check_plan_fingerprint
from .qualification import _open_denying_writers
from .recovery import PROVEN_STOP
from .status_observation import checked_commands
from .types import Refused, digest, sha


def _approved_inputs(owner, ticket, row, config, capture, check):
    if row['state'] != 'STOPPED' or row['stop_proof'] not in PROVEN_STOP or not row['proof_source'] or row['hold_reason']:
        raise Refused("ROBOT_PROVEN_STOP_REQUIRED")
    settings = getattr(owner.launcher, 'settings', None)
    if settings is None or config.get('project_ref') != ticket.project or not config.get('repository'):
        raise Refused("ROBOT_PROJECT_BINDING")
    repository = Path(config['repository']).resolve(strict=True)
    if Path(settings.repository).resolve(strict=True) != repository:
        raise Refused("ROBOT_PROJECT_BINDING")
    details = getattr(ticket.plan, 'typed_details', '')
    if details:
        original = json.loads(details)
        if original.get('repository') != str(repository) or original.get('check_policy_sha256') != check.get('plan_sha256'):
            raise Refused("ROBOT_PROJECT_BINDING")
    required, verdicts = checked_commands(ticket, settings, capture, check)
    if not required or len(verdicts) != len(required) or any(value != 'pass' for _, value in verdicts):
        raise Refused("ROBOT_CHECK_REQUIRED")
    patch = capture['patch']
    commands = tuple(settings.checks(tuple(patch['paths'])))
    if check_plan_fingerprint(settings.owner_check, commands) != check['plan_sha256']:
        raise Refused("ROBOT_CHECK_PLAN_CHANGED")
    deferred = tuple(c for c in commands if c.requires_git)
    directory = owner.journal.path.parent / 'attempts' / digest(ticket.attempt)[:12]
    if not isinstance(patch.get('path'), str):
        raise Refused("ROBOT_PATCH_LOCATION")
    path = Path(patch['path'])
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_relative_to(directory):
            raise Refused("ROBOT_PATCH_LOCATION")
        with path.open('rb') as stream:
            content = stream.read(PATCH_LIMIT + 1)
    except OSError as exc:
        raise Refused("ROBOT_PATCH_UNAVAILABLE") from exc
    if len(content) > PATCH_LIMIT or hashlib.sha256(content).hexdigest() != patch['sha256']:
        raise Refused("ROBOT_PATCH_DIGEST")
    return repository, settings, content, deferred


def _after_display(prior, decision, prefix):
    delivery = prior.get('presentation') or {}
    shown, incoming = delivery.get('message_id', ''), decision['message_id']
    return (prefix == prior['identity']['patch_sha256'] and prior['state'] == 'CHECK_FAILED_SHOWN'
            and delivery.get('session_id') == decision['session_id'] and delivery.get('chat_id') == decision['chat_id']
            and shown.isascii() and shown.isdecimal() and incoming.isascii() and incoming.isdecimal()
            and int(incoming) > int(shown) > 0)


def _redisplay(prior, prefix, decision, save, present_failure):
    if (not prior or prior['state'] not in ('CHECK_FAILED', 'CHECK_FAILED_SHOWN')
            or prefix != prior['identity']['patch_sha256']):
        raise Refused('ROBOT_SAVED_FAILURE_AND_FULL_DIGEST_REQUIRED')
    # The original pre-receipt schema did not store message IDs. Its first explicit request starts the sequence.
    earlier = prior.get('presentation_request', prior.get('command_message', {})).get('message_id', '0')
    incoming = decision['message_id']
    if not (incoming.isascii() and incoming.isdecimal() and earlier.isascii() and earlier.isdecimal()
            and int(incoming) > int(earlier)):
        raise Refused('ROBOT_NEW_PRESENTATION_REQUEST_REQUIRED')
    failed = save(dict(prior, state='CHECK_FAILED', presentation=None, presentation_request=decision))
    delivery = present_failure(failed)
    return save(dict(failed, state='CHECK_FAILED_SHOWN', presentation=delivery))


def _read_published(prior, check, repo, job):
    reader = prior.get('git_reader')
    if reader:
        git, expected = reader['path'], reader['sha256']
    else:
        legacy = locate_command('git').command
        git = legacy[0] if legacy else ''
        expected = (check or {}).get('revisions', {}).get('git')
    if not sha(expected, 64) or not Path(git).is_absolute():
        raise Refused('ROBOT_GIT_READER_UNAVAILABLE')
    with _open_denying_writers(str(git)) as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise Refused('ROBOT_GIT_READER_CHANGED')
        git_ops.publish_prepared(git, repo, job, prior['commit'], readback_only=True)


def _checked_candidate(record, profile, repo, deferred, timeout, authorize, save):
    started = False

    def starting():
        nonlocal started
        if not started:
            save(dict(record, state='CHECKING'))
            started = True

    try:
        return git_ops.run_checks(profile, repo, record['identity']['source_sha'], record['commit'], deferred,
                                  timeout, authorize, starting)
    except (Refused, OSError, subprocess.SubprocessError) as exc:
        if started or profile.active.exists():
            save(dict(record, state='CHECK_INCOMPLETE', reason=str(exc) if isinstance(exc, Refused)
                      else type(exc).__name__))
        raise Refused('ROBOT_CHECK_INCOMPLETE') from exc


def apply_checked(owner, config, job, prefix, option, authorize, present_failure, decision):
    """Serialize apply in its own lane. Only the journal and final ref CAS need the common owner fence."""
    token = owner.token
    override = option == '--accept-failed-git-checks'
    show = option == '--show-failed-git-checks'

    @contextmanager
    def fence():
        with owner.lock:
            owner._check(token)
            authorize()
            yield

    with fence():
        (ticket, row, capture, check, capture_seq, check_seq, prior,
         next_seq) = owner.journal.apply_records(token, config['project_ref'], job, prefix, saved_failure=show)

    def save(record):
        nonlocal next_seq
        with fence():
            seq = owner.journal.append_observation(token, ticket, record, expected_seq=next_seq)
            next_seq = seq + 1
        return record

    def authorize_check():
        with fence():
            pass

    repo = Path(config['repository']).resolve(strict=True)
    patch_hash = capture['patch']['sha256'] if capture else prior['identity']['patch_sha256']
    identity = dict(ticket_sha256=digest(asdict(ticket)), source_sha=ticket.plan.source_sha,
                    capture_seq=capture_seq, check_seq=check_seq, patch_sha256=patch_hash,
                    repository=str(repo), ref=f"refs/heads/symphony/{job}")
    if prior and prior.get('identity') != identity:
        if prior.get('state') not in ('CHECK_FAILED', 'CHECK_FAILED_SHOWN') or override:
            raise Refused("ROBOT_APPLY_BINDING_CHANGED")
        prior = None
    if prior and not sha(prior.get('commit'), 40):
        raise Refused("ROBOT_APPLY_STATE_UNKNOWN")
    if show:
        return _redisplay(prior, prefix, decision, save, present_failure)
    if prior and prior['state'] in ('PUBLISHING', 'APPLIED'):
        _read_published(prior, check, repo, job)
        return prior if prior['state'] == 'APPLIED' else save(dict(prior, state='APPLIED'))

    repo, settings, content, deferred = _approved_inputs(owner, ticket, row, config, capture, check)
    if override and (not prior or not _after_display(prior, decision, prefix)):
        raise Refused("ROBOT_PRESENTED_FAILURE_AND_FULL_DIGEST_REQUIRED")
    profile = settings.owner_check
    needs_workspace = not prior and any(command != DIFF_CHECK for command in deferred)
    with profile.qualified(profile.git, outside=(repo, Path(settings.attempts_root), owner.journal.path.parent),
                           workspace=needs_workspace):
        if prior:
            if prior['state'] not in ('CHECK_FAILED', 'CHECK_FAILED_SHOWN'):
                raise Refused("ROBOT_APPLY_STATE_UNKNOWN")
            if not override:
                return prior
            record = dict(prior, accepted_failed_git_checks=True, override_message=decision)
        else:
            commit = git_ops.prepare_commit(profile.git, repo, ticket.plan.source_sha, job, content, patch_hash)
            record = dict(kind='APPLY', identity=identity, commit=commit, git_checks=[],
                          git_reader=dict(path=str(profile.git), sha256=profile.git_sha256),
                          accepted_failed_git_checks=False, owner_epoch=token.epoch, command_message=decision)
            if deferred:
                record['git_checks'] = _checked_candidate(record, profile, repo, deferred, settings.check_timeout,
                                                          authorize_check, save)
                if any(item['exit_code'] for item in record['git_checks']):
                    failed = save(dict(record, state='CHECK_FAILED', presentation_request=decision,
                                       show_command=f'/symphony apply {job} {patch_hash} --show-failed-git-checks'))
                    delivery = present_failure(failed)
                    return save(dict(failed, state='CHECK_FAILED_SHOWN', presentation=delivery))
        publishing = save(dict(record, state='PUBLISHING', owner_epoch=token.epoch))
        git_ops.publish_prepared(profile.git, repo, job, record['commit'], publication_guard=fence)
        return save(dict(publishing, state='APPLIED'))
