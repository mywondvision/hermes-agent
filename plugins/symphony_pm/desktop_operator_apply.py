"""Bind the personal Desktop decision to one checked result and durable command ID."""

from contextlib import contextmanager
from pathlib import Path
import subprocess

from gateway.session_desktop_operator_store import binding_key, deny, read_record, write_record
from hermes_state_runtime import RuntimeStoreError

from .transport.robot_apply import _approved_inputs, _read_published, apply_checked
from .transport.robot_apply_git import require_absent_ref
from .transport.types import Refused, digest


class ApplyRecords:
    def __init__(self, ref, attempt, proof, payload):
        self.session = ref.session_id
        self.scope = dict(principal=proof.principal, submission_id=proof.fields[8], attempt_id=attempt['attempt_id'])
        self.action = proof.fields[2]
        self.command = payload['command_id']

    @property
    def key(self):
        return binding_key('apply-command:' + self.session, self.command)

    def read(self, conn):
        record = read_record(conn, self.key)
        if record and record['scope'] != self.scope:
            deny()
        return record

    def accept(self, conn, candidate, result):
        prior = self.read(conn)
        if prior:
            if prior['action'] != self.action or prior['candidate'] != candidate:
                deny()
            return prior, False
        key = binding_key('apply-result:' + self.session, digest(dict(scope=self.scope, candidate=candidate)))
        if read_record(conn, key):
            deny()
        record = dict(scope=self.scope, action=self.action, candidate=candidate, result=result)
        write_record(conn, self.key, record)
        write_record(conn, key, dict(command_id=self.command))
        return record, True

    def reply(self, state, **data):
        return dict(attempt_id=self.scope['attempt_id'], command_id=self.command, state=state, **data)


def checked_candidate(owner, config, attempt):
    target = attempt['project'], attempt['job'], attempt['revision']
    owner._check()
    ticket, launch, capture, check, capture_seq, check_seq = owner.journal.apply_candidate(owner.token, *target)
    if ticket.attempt != attempt['attempt_id']:
        deny()
    repo, profile, *_ = _approved_inputs(owner, ticket, launch, config, capture, check)
    return dict(attempt_id=ticket.attempt, project=ticket.project, job=ticket.job, capture_seq=capture_seq,
                check_seq=check_seq, patch_sha256=capture['patch']['sha256']), repo, profile


def _read_result(owner, records, saved, authorize):
    result = saved['result']
    if result['state'] != 'UNKNOWN' or saved['action'] != 'apply':
        return result
    candidate = saved['candidate']
    try:
        with owner.lock:
            owner._check()
            authorize()
            ticket, _, _, check, capture_seq, check_seq, prior, _ = owner.journal.apply_records(
                owner.token, candidate['project'], candidate['job'], candidate['patch_sha256'])
        if (not prior or ticket.attempt != records.scope['attempt_id']
                or (capture_seq, check_seq) != (candidate['capture_seq'], candidate['check_seq'])
                or prior.get('command_message', {}).get('message_id') != records.command):
            return result
        if prior['state'] in ('PUBLISHING', 'APPLIED'):
            _read_published(owner, prior, check, Path(prior['identity']['repository']), ticket.job)
            return records.reply('APPLIED', commit=prior['commit'], **_patch_fields(candidate))
        if prior['state'] in ('CHECK_FAILED', 'CHECK_FAILED_SHOWN'):
            return records.reply('CHECK_FAILED', **_patch_fields(candidate))
    except (Refused, OSError, subprocess.SubprocessError):
        pass  # A read that cannot prove the effect leaves the original command unresolved.
    return result


def _patch_fields(candidate):
    return {key: candidate[key] for key in ('job', 'capture_seq', 'check_seq', 'patch_sha256')}


def apply_operation(approval, db, ref, attempt, proof, payload, current, commit_guard):
    owner = approval.owner
    config = approval.ctx.get_config('typed_approval', {})
    if not isinstance(config, dict) or config.get('project_ref') != attempt['project']:
        deny()
    action = proof.fields[2]
    if action == 'apply_info':
        with owner.lock:
            return checked_candidate(owner, config, attempt)[0]
    records = ApplyRecords(ref, attempt, proof, payload)

    def read_saved():
        with db._read_ctx() as conn:
            current(conn)
            return records.read(conn)

    def authorize():
        with db._read_ctx() as conn:
            current(conn)

    if action == 'apply_result':
        saved = read_saved()
        return _read_result(owner, records, saved, authorize) if saved else records.reply('UNKNOWN')
    with approval.apply_lock:
        saved = read_saved()
        if saved:
            if saved['action'] != action or _patch_fields(saved['candidate']) != _patch_fields(payload):
                deny()
            return _read_result(owner, records, saved, authorize)
        with owner.lock:
            candidate, repo, profile = checked_candidate(owner, config, attempt)
            if _patch_fields(candidate) != _patch_fields(payload):
                deny()
            result = records.reply('UNKNOWN', **_patch_fields(candidate))
            if action == 'archive':
                require_absent_ref(profile.git, repo, candidate['job'])
                result['state'] = 'ARCHIVED'

            def accept(conn):
                current(conn)
                return records.accept(conn, candidate, result)

            saved, first = db._execute_write(accept, commit_guard=commit_guard)
        if not first or action == 'archive':
            return saved['result']
        return _apply(approval, db, ref, attempt, current, commit_guard, config, records, candidate, result)


def _apply(approval, db, ref, attempt, current, commit_guard, config, records, candidate, result):
    owner = approval.owner

    @contextmanager
    def authority_guard():
        with db._read_ctx() as conn, commit_guard(admitted=True, conn=conn):
            current(conn, admitted=True)
            if approval.ctx.get_config('typed_approval', {}) != config:
                deny()
            yield

    def present_failure(_):
        raise Refused('ROBOT_GIT_CHECKS_FAILED')

    expected = (attempt['attempt_id'], attempt['revision'], candidate['capture_seq'],
                candidate['check_seq'], candidate['patch_sha256'])
    decision = dict(message_id=records.command, session_id=ref.session_id, chat_id=ref.session_id)
    try:
        applied = apply_checked(owner, config, candidate['job'], candidate['patch_sha256'], None, lambda: None,
                                present_failure, decision, expected_result=expected, authority_guard=authority_guard)
        if applied['state'] == 'APPLIED':
            result = records.reply('APPLIED', commit=applied['commit'], **_patch_fields(candidate))
    except Refused as error:
        if str(error) == 'ROBOT_GIT_CHECKS_FAILED':
            result = records.reply('CHECK_FAILED', **_patch_fields(candidate))
    except (RuntimeStoreError, OSError, subprocess.SubprocessError):
        pass

    def finish(conn):
        # Store proof of a completed effect even if delivery was lost immediately after publication.
        saved = records.read(conn)
        write_record(conn, records.key, dict(saved, result=result))

    if result['state'] != 'UNKNOWN':
        db._execute_write(finish)
    return result
