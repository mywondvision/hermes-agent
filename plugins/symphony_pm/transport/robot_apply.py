"""Owner-authorized Robot apply, with a durable intent before Git creates the local branch."""
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess

from . import robot_apply_git as git_ops
from .recapture import read_bound_patch
from hermes_platform.resolver import locate_command
from .check_command import DIFF_CHECK
from .status_observation import checked_commands
from .types import Refused, digest, sha


def _approved_inputs(owner, ticket, row, config, capture, check):
    if not owner.journal.proven_stop(row) or row['hold_reason']:
        raise Refused("ROBOT_PROVEN_STOP_REQUIRED")
    settings = getattr(owner.launcher, 'settings', None)
    if settings is None or config.get('project_ref') != ticket.project or not config.get('repository'):
        raise Refused("ROBOT_PROJECT_BINDING")
    repository = Path(config['repository']).resolve(strict=True)
    if Path(settings.repository).resolve(strict=True) != repository:
        raise Refused("ROBOT_PROJECT_BINDING")
    details = getattr(ticket.plan, 'typed_details', '')
    required, verdicts = checked_commands(owner, ticket, row, capture, check)
    if not required or len(verdicts) != len(required) or any(value != 'pass' for _, value in verdicts):
        raise Refused("ROBOT_CHECK_REQUIRED")
    patch = capture['patch']
    profile, commands, policy_digest, timeout = owner.robot_check_plan(ticket, tuple(patch['paths']))
    if policy_digest != check.get('plan_sha256', check.get('revisions', {}).get('profile')):
        raise Refused("ROBOT_CHECK_PLAN_CHANGED")
    if details:
        original = json.loads(details)
        if original.get('repository') != str(repository) or original.get('check_policy_sha256') != policy_digest:
            raise Refused("ROBOT_PROJECT_BINDING")
    deferred = tuple(c for c in commands if c.requires_git)
    directory = owner.journal.path.parent / 'attempts' / digest(ticket.attempt)[:12]
    content = read_bound_patch(directory, patch)
    return repository, profile, timeout, content, deferred


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


def _read_published(owner, prior, check, repo, job):
    reader = prior.get('git_reader')
    if reader:
        git, expected = reader['path'], reader['sha256']
    else:
        legacy = locate_command('git').command
        git = legacy[0] if legacy else ''
        expected = (check or {}).get('revisions', {}).get('git')
    if not sha(expected, 64) or not Path(git).is_absolute():
        raise Refused('ROBOT_GIT_READER_UNAVAILABLE')
    with owner.open_robot_git(git) as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise Refused('ROBOT_GIT_READER_CHANGED')
        git_ops.publish_prepared(git, repo, job, prior['commit'], readback_only=True)


def _checked_candidate(record, profile, repo, deferred, timeout, authorize, save, project_check=None):
    started = False

    def starting(invocation=None):
        nonlocal started
        if invocation is not None:
            record.update(git_invocation=invocation)
            record.pop('terminal', None)
        if not started or invocation is not None:
            save(dict(record, state='CHECKING'))
            started = True

    try:
        return git_ops.run_checks(profile, repo, record['identity']['source_sha'], record['commit'], deferred,
                                  timeout, authorize, starting, project_check)
    except (Refused, OSError, subprocess.SubprocessError) as exc:
        if 'terminal' not in record and (started or (getattr(profile, 'active', None) is not None and profile.active.exists())):
            save(dict(record, state='CHECK_INCOMPLETE', reason=str(exc) if isinstance(exc, Refused)
                      else type(exc).__name__))
        raise Refused('ROBOT_CHECK_INCOMPLETE') from exc


def apply_checked(owner, config, job, prefix, option, authorize, present_failure, decision, *,
                  expected_result=None, authority_guard=nullcontext):
    """Serialize apply in its own lane. Only the journal and final ref CAS need the common owner fence."""
    token = owner.token
    override = option == '--accept-failed-git-checks'
    show = option == '--show-failed-git-checks'

    @contextmanager
    def fence():
        with owner.lock, authority_guard():
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
    if expected_result is not None and expected_result != (
            ticket.attempt, ticket.revision, capture_seq, check_seq, patch_hash):
        raise Refused('ROBOT_APPLY_BINDING_CHANGED')
    if expected_result is not None and prior and prior.get('command_message') != decision:
        raise Refused('ROBOT_APPLY_BINDING_CHANGED')
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
        _read_published(owner, prior, check, repo, job)
        return prior if prior['state'] == 'APPLIED' else save(dict(prior, state='APPLIED'))

    repo, profile, timeout, content, deferred = _approved_inputs(owner, ticket, row, config, capture, check)
    if override and (not prior or not _after_display(prior, decision, prefix)):
        raise Refused("ROBOT_PRESENTED_FAILURE_AND_FULL_DIGEST_REQUIRED")
    recovered = prior is not None and prior['state'] == 'CHECK_RECOVERED'
    if recovered:
        spent = prior.get('command_message') or {}
        earlier = spent.get('message_id', '')
        incoming = decision['message_id']
        # The gateway verifies the current owner session. Discord message IDs remain ordered across its rotations.
        if not (spent.get('chat_id') == decision['chat_id']
                and earlier.isascii() and earlier.isdecimal() and incoming.isascii() and incoming.isdecimal()
                and int(incoming) > int(earlier) > 0):
            raise Refused('ROBOT_NEW_APPLY_COMMAND_REQUIRED')
    needs_workspace = (not prior or recovered) and any(command != DIFF_CHECK for command in deferred)
    with owner.robot_check_context(profile, repo, needs_workspace):
        if prior and not recovered:
            if prior['state'] not in ('CHECK_FAILED', 'CHECK_FAILED_SHOWN'):
                raise Refused("ROBOT_APPLY_STATE_UNKNOWN")
            if not override:
                return prior
            record = dict(prior, accepted_failed_git_checks=True, override_message=decision)
        else:
            if recovered:
                record = dict(prior, command_message=decision, owner_epoch=token.epoch)
            else:
                with fence() if expected_result is not None else nullcontext():
                    commit = git_ops.prepare_commit(profile.git, repo, ticket.plan.source_sha, job, content, patch_hash)
                record = dict(kind='APPLY', identity=identity, commit=commit, git_checks=[],
                              git_reader=dict(path=str(profile.git), sha256=profile.git_sha256),
                              accepted_failed_git_checks=False, owner_epoch=token.epoch, command_message=decision)
            if deferred:
                completed = list(record['git_checks'])
                if ([r['name'] for r in completed] != [c.name for c in deferred[:len(completed)]]
                        or any(r['source_sha'] != ticket.plan.source_sha or r['candidate_sha'] != record['commit']
                               for r in completed)):
                    raise Refused('ROBOT_APPLY_BINDING_CHANGED')
                project_check = None
                if hasattr(owner, 'robot_project_check'):
                    def project_check(command, starting, previous):
                        from .linux_robot_check import settled_record
                        record['git_checks'] = completed + list(previous)
                        def settle(invocation, terminal):
                            receipt = settled_record(record, invocation, terminal)
                            save(receipt)
                            record.update(receipt)
                        return owner.robot_project_check(ticket, record, repo, command, timeout, starting, fence, settle)
                remaining = deferred[len(completed):]
                if remaining:
                    record['git_checks'] = completed + _checked_candidate(record, profile, repo, remaining, timeout,
                                                                          authorize_check, save, project_check)
                if any(item['exit_code'] for item in record['git_checks']):
                    failed = save(dict(record, state='CHECK_FAILED', presentation_request=decision,
                                       show_command=f'/symphony apply {job} {patch_hash} --show-failed-git-checks'))
                    delivery = present_failure(failed)
                    return save(dict(failed, state='CHECK_FAILED_SHOWN', presentation=delivery))
        publishing = save(dict(record, state='PUBLISHING', owner_epoch=token.epoch))
        git_ops.publish_prepared(profile.git, repo, job, record['commit'], publication_guard=fence)
        return save(dict(publishing, state='APPLIED'))
