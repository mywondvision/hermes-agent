"""Single-owner durable intent journal. No scheduler, native effect, retry or fixture import.

Connections are per operation/thread. Every write checks the current incarnation
and writer epoch inside the same transaction. An uncertain commit never creates a
replacement launch/cancel ticket. OS ownership is established by LifecycleOwner.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import uuid

from hermes_cli.plugin_request_binding import RequestIdentity, canonical_home
from hermes_dir_acl import make_user_dir
from tools import symphony_pm_adapter as pm
from .schema import initialize
from .types import (
    ExecutionPlan, LaunchTicket, Permit, ProcessIdentity, Refused, ServingIdentity, WriterToken,
    binding_from_json, digest, finite, identifier,
)


def encode(value):
    return pm.canonical_bytes(value).decode('utf-8')


def permit_from_json(value):
    value = dict(value)
    value['identity'] = RequestIdentity(**value['identity'])
    value['plan'] = ExecutionPlan(**value['plan'])
    return Permit(**value)


def ticket_from_json(value):
    value = dict(value)
    value['plan'] = ExecutionPlan(**value['plan'])
    return LaunchTicket(**value)


def reconcile_key(ticket):
    return digest([ticket.namespace, ticket.project, ticket.job, ticket.revision, ticket.attempt, 'create-reconcile'])


class Journal:
    def __init__(self, home, namespace, *, migrate_legacy=False):
        if not identifier(namespace):
            raise Refused('NAMESPACE')
        self.home, self.namespace = canonical_home(home), namespace
        self.path = Path(self.home)/'plugin-data'/'symphony_pm'/digest(namespace)[:32]/'journal.sqlite3'
        if not self.path.resolve().is_relative_to(Path(self.home)):
            raise Refused('JOURNAL_PATH')
        make_user_dir(self.path.parent, mode=0o700, parents=True, exist_ok=True)
        with self._connection() as db:
            initialize(db, migrate_legacy=migrate_legacy)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            yield db
        except sqlite3.Error as exc:
            raise Refused('STORE_OUTCOME_UNKNOWN') from exc
        finally:
            db.close()

    @contextmanager
    def _transaction(self, token=None):
        with self._connection() as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            try:
                if token is not None:
                    self._authorize(db, token)
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _authorize(self, db, token):
        row = db.execute('SELECT * FROM serving_owner WHERE namespace=?', (self.namespace,)).fetchone()
        if (type(token) is not WriterToken or row is None
                or token.profile_home != self.home or token.namespace != self.namespace
                or json.loads(row['identity'])['incarnation'] != token.incarnation
                or row['controller'] != token.controller or row['epoch'] != token.epoch):
            raise Refused('WRITER_FENCED')
        return row

    def previous_owner(self):
        with self._connection() as db:
            row = db.execute('SELECT identity FROM serving_owner WHERE namespace=?', (self.namespace,)).fetchone()
            return json.loads(row[0]) if row else None

    def claim(self, identity: ServingIdentity, controller, expected_previous):
        """Called only while the verified exclusive OS ownership guard is held."""
        identity.validate()
        if identity.profile_home != self.home or identity.namespace != self.namespace or not identifier(controller):
            raise Refused('OWNER_SCOPE')
        with self._transaction() as db:
            if db.execute('SELECT 1 FROM serving_recovery_hold WHERE namespace IN (?,?)',
                          (self.namespace, '*')).fetchone():
                raise Refused('MIGRATION_RECONCILIATION_REQUIRED')
            row = db.execute('SELECT * FROM serving_owner WHERE namespace=?', (self.namespace,)).fetchone()
            previous = json.loads(row['identity']) if row else None
            if previous != expected_previous:
                raise Refused('PREDECESSOR_CHANGED')
            writer = db.execute('SELECT epoch FROM writers WHERE namespace=?', (self.namespace,)).fetchone()
            if writer and (row is None or writer['epoch'] != row['epoch']):
                raise Refused('ORPHAN_WRITER')
            epoch = row['epoch']+1 if row else 1
            generation = row['observer_generation']+1 if row else 1
            db.execute('INSERT OR REPLACE INTO serving_owner VALUES(?,?,?,?,?,0)',
                       (self.namespace, encode(asdict(identity)), controller, epoch, generation))
            db.execute('INSERT OR REPLACE INTO writers VALUES(?,?,?,?)',
                       (self.namespace, identity.incarnation, epoch, 0.0))
        return WriterToken(self.home, self.namespace, identity.incarnation, controller, epoch)

    def rotate_controller(self, token, controller):
        if not identifier(controller):
            raise Refused('CONTROLLER_IDENTITY')
        with self._transaction(token) as db:
            epoch = token.epoch+1
            db.execute('UPDATE serving_owner SET controller=?,epoch=?,observer_healthy=0 WHERE namespace=?',
                       (controller, epoch, self.namespace))
            db.execute('UPDATE writers SET epoch=? WHERE namespace=?', (epoch, self.namespace))
        return WriterToken(self.home, self.namespace, token.incarnation, controller, epoch)

    def observer_state(self, token, *, healthy, replacement=False):
        with self._transaction(token) as db:
            row = self._authorize(db, token)
            generation = row['observer_generation'] + int(replacement)
            db.execute('UPDATE serving_owner SET observer_generation=?,observer_healthy=? WHERE namespace=?',
                       (generation, int(healthy), self.namespace))
            return generation

    def _healthy(self, db, token):
        row = self._authorize(db, token)
        if not row['observer_healthy']:
            raise Refused('OBSERVER_UNAVAILABLE')
        return row['observer_generation']

    def record_permit(self, token, permit: Permit, approval_id, approval_digest, now):
        permit.validate(now)
        if (permit.identity.profile_home != self.home or permit.identity.namespace != self.namespace
                or not approval_id or not approval_digest):
            raise Refused('PERMIT_SCOPE')
        body = encode(asdict(permit))
        with self._transaction(token) as db:
            old = db.execute('SELECT * FROM serving_permits WHERE ref=?', (permit.ref,)).fetchone()
            if old:
                if (old['body'], old['approval_id'], old['approval_digest']) != (body, approval_id, approval_digest):
                    raise Refused('PERMIT_CONFLICT')
                return
            db.execute('INSERT INTO serving_permits VALUES(?,?,?,?,?,?,0)',
                       (permit.ref, body, permit.binding_digest, digest(asdict(permit.identity)), approval_id, approval_digest))

    def find_permit(self, token, identity, now):
        with self._transaction(token) as db:
            # Identity is host-created; no grant is minted by a matching model payload.
            found = []
            for row in db.execute('SELECT body FROM serving_permits WHERE identity_binding=?',
                                  (digest(asdict(identity)),)):
                permit = permit_from_json(json.loads(row['body']))
                if permit.identity == identity:
                    permit.validate(now)
                    found.append(permit)
            if len(found) != 1:
                raise Refused('EXACT_PERMIT_NOT_FOUND')
            return found[0]

    def _permit(self, db, request, context, now):
        row = db.execute('SELECT * FROM serving_permits WHERE ref=?', (context.grant_ref,)).fetchone()
        if row is None:
            raise Refused('PERMIT_ABSENT')
        permit = permit_from_json(json.loads(row['body']))
        permit.validate(now)
        if (permit.binding_digest != row['binding'] or permit.identity.profile_home != self.home
                or permit.identity.namespace != self.namespace or permit.project_ref != request.project_ref
                or permit.identity.request_id != request.request_id
                or permit.identity.payload_digest != request.payload_digest
                or permit.request_digest != request.request_digest or permit.operation != request.operation
                or (permit.issuer_ref, permit.policy_ref, permit.contract_digest, permit.scope_revision) !=
                   (context.issuer_ref, context.policy_ref, context.contract_digest, context.approved_scope_revision)):
            raise Refused('PERMIT_REQUEST_BINDING')
        return permit, row

    def reserve_launch(self, token, request, context, now):
        with self._transaction(token) as db:
            self._healthy(db, token)
            permit, permit_row = self._permit(db, request, context, now)
            binding = digest([request.operation, request.payload_digest, permit.binding_digest])
            prior = db.execute('SELECT * FROM operations WHERE namespace=? AND project=? AND request_id=?',
                               (self.namespace, request.project_ref, request.request_id)).fetchone()
            if prior:
                if prior['binding'] != binding:
                    raise Refused('CONFLICT')
                ticket, state = self._launch(db, prior['project'], prior['job'], prior['revision'])
                return ticket, state['state'], False
            if db.execute('SELECT 1 FROM serving_launches WHERE namespace=?', (self.namespace,)).fetchone():
                raise Refused('UNSETTLED_PRIOR_LAUNCH_HOLD')
            if permit_row['used'] >= permit.plan.attempt_budget:
                raise Refused('ATTEMPT_BUDGET_SPENT')
            job, attempt = 'job-'+uuid.uuid4().hex, 'attempt-'+uuid.uuid4().hex
            revision = digest([job, binding])
            ticket = LaunchTicket(self.namespace, request.project_ref, job, revision, attempt,
                                  request.request_digest, permit.ref, permit.plan, permit.identity.session_id)
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,NULL)',
                       (self.namespace, request.project_ref, job, revision, permit.contract_digest))
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,NULL)',
                       (self.namespace, request.project_ref, request.request_id, binding, job, revision))
            db.execute("INSERT INTO serving_launches VALUES(?,?,?,?,?,'LAUNCH_INTENT',NULL,NULL)",
                       (self.namespace, request.project_ref, job, attempt, encode(asdict(ticket))))
            db.execute('UPDATE serving_permits SET used=used+1 WHERE ref=?', (permit.ref,))
            return ticket, 'LAUNCH_INTENT', True

    def _launch(self, db, project, job, revision):
        row = db.execute('SELECT l.*,j.revision FROM serving_launches l JOIN jobs j USING(namespace,project,job) '
                         'WHERE l.namespace=? AND l.project=? AND l.job=?',
                         (self.namespace, project, job)).fetchone()
        if row is None or row['revision'] != revision:
            raise Refused('JOB_IDENTITY')
        return ticket_from_json(json.loads(row['ticket'])), dict(row)

    def read_launch(self, token, project, job, revision):
        with self._transaction(token) as db:
            return self._launch(db, project, job, revision)

    def authorize_control(self, token, request, context, now):
        with self._transaction(token) as db:
            permit, _ = self._permit(db, request, context, now)
            ticket, row = self._launch(db, request.project_ref, request.job_ref, request.expected_job_revision)
            if (permit.job_ref != ticket.job or permit.job_revision != ticket.revision
                    or permit.plan != ticket.plan or permit.identity.session_id != ticket.session
                    or row['binding'] is None):
                raise Refused('CONTROL_TARGET_BINDING')
            binding = digest([request.operation, request.payload_digest, permit.binding_digest])
            prior = db.execute('SELECT binding FROM operations WHERE namespace=? AND project=? AND request_id=?',
                               (self.namespace, request.project_ref, request.request_id)).fetchone()
            if prior and prior['binding'] != binding:
                raise Refused('CONFLICT')
            if not prior:
                db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?)',
                           (self.namespace, request.project_ref, request.request_id, binding,
                            ticket.job, ticket.revision, None))
            return binding_from_json(json.loads(row['binding']))

    def unfinished(self, token):
        with self._transaction(token) as db:
            return [dict(r) for r in db.execute('SELECT * FROM serving_launches WHERE namespace=?', (self.namespace,))]

    def claim_create(self, token, ticket):
        """Spend the create slot before OS entry, including an uncertain create failure."""
        with self._transaction(token) as db:
            self._healthy(db, token)
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or row['state'] != 'LAUNCH_INTENT' or row['binding'] is not None:
                raise Refused('CREATE_SLOT_SPENT')
            db.execute("UPDATE serving_launches SET state='CREATE_INTENT' WHERE namespace=? AND project=? AND job=?",
                       (self.namespace, ticket.project, ticket.job))

    def dispatch_cancel(self, token, binding, generation, action):
        """Hold the writer transaction across native entry; no takeover can interleave."""
        with self._transaction(token) as db:
            if self._healthy(db, token) != generation:
                raise Refused('STALE_OBSERVER')
            row = db.execute('SELECT * FROM effects WHERE effect_key=?', (binding.effect_key,)).fetchone()
            if (row is None or row['state'] != 'DISPATCH_INTENT'
                    or row['dispatch_epoch'] != token.epoch or row['target'] != encode(asdict(binding))):
                raise Refused('NATIVE_CANCEL_FENCED')
            return action()

    def dispatch_resume(self, token, ticket, generation, now, action):
        """Fence resume against writer rotation as well as the durable resume intent."""
        with self._transaction(token) as db:
            if self._healthy(db, token) != generation:
                raise Refused('STALE_OBSERVER')
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or row['state'] != 'RESUME_INTENT' or row['binding'] is None:
                raise Refused('RESUME_WITHHELD')
            permit_row = db.execute('SELECT body FROM serving_permits WHERE ref=?', (ticket.permit_ref,)).fetchone()
            if permit_row is None:
                raise Refused('PERMIT_ABSENT')
            permit_from_json(json.loads(permit_row['body'])).validate(now)
            return action()

    def bind_native(self, token, ticket, binding):
        binding.validate()
        if binding.ticket != ticket:
            raise Refused('LAUNCH_BINDING')
        body = encode(asdict(binding))
        with self._transaction(token) as db:
            self._healthy(db, token)
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or row['state'] != 'CREATE_INTENT' or row['binding'] is not None:
                raise Refused('LAUNCH_ALREADY_BOUND')
            db.execute("UPDATE serving_launches SET state='BOUND',binding=? WHERE namespace=? AND project=? AND job=?",
                       (body, self.namespace, ticket.project, ticket.job))
            db.execute('UPDATE jobs SET target=? WHERE namespace=? AND project=? AND job=?',
                       (body, self.namespace, ticket.project, ticket.job))
            for process in binding.known_processes:
                db.execute('INSERT INTO serving_members VALUES(?,?,?)',
                           (ticket.attempt, process.pid, process.creation))

    def mark_resume_intent(self, token, ticket):
        with self._transaction(token) as db:
            self._healthy(db, token)
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or row['state'] != 'BOUND' or row['binding'] is None:
                raise Refused('RESUME_WITHHELD')
            db.execute("UPDATE serving_launches SET state='RESUME_INTENT' WHERE namespace=? AND project=? AND job=?",
                       (self.namespace, ticket.project, ticket.job))

    def verify_launch_authority(self, token, ticket, now):
        with self._transaction(token) as db:
            row = db.execute('SELECT * FROM serving_permits WHERE ref=?', (ticket.permit_ref,)).fetchone()
            if row is None:
                raise Refused('PERMIT_ABSENT')
            permit = permit_from_json(json.loads(row['body']))
            permit.validate(now)
            if (permit.binding_digest != row['binding'] or permit.plan != ticket.plan
                    or permit.request_digest != ticket.request_digest
                    or permit.identity.session_id != ticket.session or row['used'] != 1):
                raise Refused('LAUNCH_PERMIT_BINDING')

    def cancel_intent(self, token, binding, cause, request_id, detail, now):
        if cause not in ('PM_CANCEL', 'TIMEOUT', 'EMERGENCY_CLEANUP') or not finite(now):
            raise Refused('CANCEL_CAUSE')
        binding.validate()
        with self._transaction(token) as db:
            self._healthy(db, token)
            ticket = binding.ticket
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            body = encode(asdict(binding))
            if actual != ticket or row['binding'] != body:
                raise Refused('CANCEL_TARGET_BINDING')
            db.execute('INSERT OR IGNORE INTO serving_cancel_causes VALUES(?,?,?,?,?)',
                       (binding.effect_key, request_id, cause, detail, now))
            prior = db.execute('SELECT * FROM effects WHERE effect_key=?', (binding.effect_key,)).fetchone()
            if prior:
                return False
            db.execute("INSERT INTO effects VALUES(?,?,'DISPATCH_INTENT',?,?,?,NULL,NULL)",
                       (binding.effect_key, body, token.epoch, now, 'exact-receiver-binding'))
            return True

    def note_cancel_cause(self, token, binding, cause, request_id, detail, now):
        if cause not in ('PM_CANCEL', 'TIMEOUT', 'EMERGENCY_CLEANUP') or not finite(now):
            raise Refused('CANCEL_CAUSE')
        with self._transaction(token) as db:
            ticket, row = self._launch(db, binding.ticket.project, binding.ticket.job, binding.ticket.revision)
            if ticket != binding.ticket or row['binding'] != encode(asdict(binding)):
                raise Refused('CANCEL_TARGET_BINDING')
            db.execute('INSERT OR IGNORE INTO serving_cancel_causes VALUES(?,?,?,?,?)',
                       (binding.effect_key, request_id, cause, detail, now))

    def finish_cancel(self, token, binding, state):
        if state not in ('CANCEL_REQUESTED', 'UNKNOWN'):
            raise Refused('CANCEL_STATE')
        with self._transaction(token) as db:
            db.execute("UPDATE effects SET state=? WHERE effect_key=? AND target=? AND state='DISPATCH_INTENT'",
                       (state, binding.effect_key, encode(asdict(binding))))

    def effect(self, token, binding):
        with self._transaction(token) as db:
            row = db.execute('SELECT * FROM effects WHERE effect_key=?', (binding.effect_key,)).fetchone()
            return dict(row) if row else None

    def save_readback(self, token, binding, generation, now, body):
        with self._transaction(token) as db:
            actual_generation = self._authorize(db, token)['observer_generation']
            if generation != actual_generation or not finite(now):
                raise Refused('STALE_OBSERVER')
            db.execute('INSERT OR REPLACE INTO serving_readbacks VALUES(?,?,?,?,?)',
                       (binding.effect_key, token.epoch, generation, now, encode(body)))

    def record_reconcile(self, token, ticket, generation, now, body):
        """Evidence only: the launch state, permit use and effects stay exactly as the predecessor left them."""
        if not finite(now):
            raise Refused('RECONCILE_TIME')
        with self._transaction(token) as db:
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or row['binding'] is not None or row['state'] not in ('LAUNCH_INTENT', 'CREATE_INTENT'):
                raise Refused('RECONCILE_TARGET')
            # First observation wins: a later successor must not overwrite what the first one recorded.
            db.execute('INSERT OR IGNORE INTO serving_readbacks VALUES(?,?,?,?,?)',
                       (reconcile_key(ticket), token.epoch, generation, now, encode(body)))

    def reconcile_record(self, token, ticket):
        with self._transaction(token) as db:
            row = db.execute('SELECT * FROM serving_readbacks WHERE effect_key=?', (reconcile_key(ticket),)).fetchone()
            return dict(row) if row else None

    def known_members(self, token, binding):
        with self._transaction(token) as db:
            return tuple(ProcessIdentity(row['pid'], row['creation']) for row in db.execute(
                'SELECT pid,creation FROM serving_members WHERE attempt=? ORDER BY pid,creation',
                (binding.ticket.attempt,)))

    def record_members(self, token, binding, processes):
        if not processes or not all(type(p) is ProcessIdentity and p.valid() for p in processes):
            raise Refused('EMPTY_PROCESS_COVERAGE')
        with self._transaction(token) as db:
            ticket, row = self._launch(db, binding.ticket.project, binding.ticket.job, binding.ticket.revision)
            if row['binding'] != encode(asdict(binding)):
                raise Refused('PROCESS_TARGET_BINDING')
            for process in processes:
                db.execute('INSERT OR IGNORE INTO serving_members VALUES(?,?,?)',
                           (ticket.attempt, process.pid, process.creation))

    def record_result(self, token, ticket, result):
        body = encode(result)
        with self._transaction(token) as db:
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or row['binding'] is None:
                raise Refused('RESULT_TARGET_BINDING')
            if row['result'] is not None and row['result'] != body:
                raise Refused('RESULT_CONFLICT')
            db.execute('UPDATE serving_launches SET result=? WHERE namespace=? AND project=? AND job=?',
                       (body, self.namespace, ticket.project, ticket.job))
