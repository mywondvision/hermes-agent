"""Durable admission and reconciliation of a personal Desktop STOP."""

from dataclasses import asdict

from gateway.session_desktop_operator_store import binding_key, deny, read_record, write_record
from hermes_state_runtime import RuntimeStoreError

from .transport import status_observation
from .transport.types import Refused


class StopRecords:
    def __init__(self, ref, proof, payload, current):
        self.session = 'stop:' + ref.session_id
        self.current = current
        self.action = proof.fields[2]
        self.command = payload['command_id']
        self.previous = payload.get('previous_command_id')
        self.reason = payload.get('reason')
        self.identity = dict(principal=proof.principal, submission_id=proof.fields[8],
                             attempt_id=payload['attempt_id'])

    def read(self, conn, command):
        self.current(conn)
        saved = read_record(conn, binding_key(self.session, command))
        if saved is not None and saved['identity'] != self.identity:
            deny()
        return saved

    def write(self, conn, command, record):
        write_record(conn, binding_key(self.session, command), record)

    def result(self, state='UNKNOWN', **fields):
        return dict(attempt_id=self.identity['attempt_id'], command_id=self.command, state=state, **fields)

    def replay(self, saved):
        if (saved['reason'] != self.reason or (self.action != 'stop_reconcile'
                and saved.get('previous_command_id') != self.previous)):
            raise RuntimeStoreError('admission_conflict')
        return saved['result'], False

    def accept(self, conn):
        saved = self.read(conn, self.command)
        if saved is not None:
            return self.replay(saved)
        result = self.result()
        record = dict(identity=self.identity, reason=self.reason, result=result)
        if self.action == 'stop_reconcile':
            result.update(state='NOT_ACCEPTED', dispatched=False, local_stopped=False)
            record['sealed'] = True
        if self.previous is not None:
            previous = self.read(conn, self.previous)
            if (not previous or previous.get('sealed') is not True or previous.get('replaced_by') is not None
                    or previous['result']['state'] != 'NOT_ACCEPTED'):
                deny()
            self.write(conn, self.previous, dict(previous, replaced_by=self.command))
            result['previous_command_id'] = self.previous
            record['previous_command_id'] = self.previous
        self.write(conn, self.command, record)
        return result, self.action != 'stop_reconcile'


def _check_target(owner, target, attempt, *, require_running):
    owner._check()
    ticket, row = owner.journal.read_launch(owner.token, *target)
    if ticket.attempt != attempt or row['state'] in ('NOT_STARTED', 'STOPPED', 'RELEASED_BY_OWNER'):
        raise RuntimeStoreError('stale_stop_target')
    if require_running and status_observation.phase(owner, owner.status_row(ticket, row)) != 'RUNNING':
        deny()


def _has_cause(owner, target, attempt, command):
    owner._check()
    with owner.journal._transaction(owner.token) as conn:
        ticket, _ = owner.journal._launch(conn, *target)
        if ticket.attempt != attempt:
            deny()
        # Native dispatch for this route follows the admitted command and its journal cause.
        return conn.execute('SELECT 1 FROM serving_cancel_causes WHERE request_id=? LIMIT 1',
                            ('operator-' + command,)).fetchone() is not None


def _prepare(owner, db, records, target, commit_guard):
    attempt = records.identity['attempt_id']
    # Check the owner before writing the intent. The lifecycle cannot advance the target
    # between the gate and persistence. Do not take its lock inside the SessionDB writer.
    with owner.lock:
        with db._read_ctx() as conn:
            saved = records.read(conn, records.command)
        if saved is not None:
            return records.replay(saved)
        try:
            if records.action == 'stop_reconcile':
                if _has_cause(owner, target, attempt, records.command):
                    return records.result(), False
            else:
                _check_target(owner, target, attempt, require_running=records.previous is not None)
        except Refused:
            return records.result(), False
        try:
            return db._execute_write(records.accept, commit_guard=commit_guard)
        except RuntimeStoreError as error:
            if error.reason != 'stop_reconcile_unproven':
                raise
            return records.result(), False


def _dispatch(owner, db, records, target, commit_guard, result):
    revoked = False

    def dispatch_guard(native_entry):
        nonlocal revoked
        entered = False
        try:
            # Recovery cannot replace the window while this final native boundary is held.
            # This callback is outside SessionDB's retryable write.
            with db._read_ctx() as conn, commit_guard():
                records.current(conn)
                entered = True
                return native_entry()
        except (PermissionError, RuntimeStoreError, Refused):
            if not entered:
                revoked = True
            raise

    try:
        with owner.lock:
            attempt = records.identity['attempt_id']
            _check_target(owner, target, attempt, require_running=records.previous is not None)
            cancel = owner.stop_by_owner(*target, request_id='operator-' + records.command, detail=records.reason,
                                         dispatch_guard=dispatch_guard, expected_attempt=attempt)
        result.update({key: value for key, value in asdict(cancel).items() if key in ('state', 'dispatched', 'local_stopped')})
    except (PermissionError, RuntimeStoreError, Refused):
        pass  # A recorded intent is read after uncertainty and never re-enters the receiver.
    if revoked:
        result.update(state='REVOKED_BEFORE_CANCEL', dispatched=False, local_stopped=False)

    def finish(conn):
        saved = read_record(conn, binding_key(records.session, records.command))
        if saved is not None and saved['identity'] == records.identity:
            records.write(conn, records.command, dict(saved, result=result))

    db._execute_write(finish)
    return result


def stop_operation(owner, db, ref, proof, payload, current, commit_guard, target):
    records = StopRecords(ref, proof, payload, current)
    if records.action == 'stop_result':
        with db._read_ctx() as conn:
            saved = records.read(conn, records.command)
        return saved['result'] if saved else records.result()
    result, first = _prepare(owner, db, records, target, commit_guard)
    if not first:
        return result
    return _dispatch(owner, db, records, target, commit_guard, result)
