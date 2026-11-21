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
import threading
import uuid

from hermes_cli.plugin_request_binding import RequestIdentity, canonical_home
from hermes_dir_acl import make_user_dir
from plugins.symphony_pm import adapter as pm
from plugins.symphony_pm.contracts import LaunchExpired
from .schema import initialize
from .recovery import PROVEN_STOP, PredecessorProof, recovered_state
from .types import (
    LaunchTicket, Permit, ProcessIdentity, Refused, ServingIdentity, WriterToken,
    binding_from_json, digest, finite, identifier, plan_from_json, sha,
)


def encode(value):
    return pm.canonical_bytes(value).decode('utf-8')


def permit_from_json(value):
    value = dict(value)
    value['identity'] = RequestIdentity(**value['identity'])
    value['plan'] = plan_from_json(value['plan'])
    return Permit(**value)


def ticket_from_json(value):
    value = dict(value)
    value['plan'] = plan_from_json(value['plan'])
    return LaunchTicket(**value)


def reconcile_key(ticket):
    return digest([ticket.namespace, ticket.project, ticket.job, ticket.revision, ticket.attempt, 'create-reconcile'])


_FREE = """(state IN ('NOT_STARTED','RELEASED_BY_OWNER') OR
           (state='STOPPED' AND stop_proof IN ('OWNER_DEAD','BOOT_CHANGED','JOB_EMPTY') AND proof_source IS NOT NULL))"""
_BOOTSTRAP = object()


class Journal:
    def __init__(self, home, namespace, *, migrate_legacy=False, migrate_v1=False,
                 ownership_contract=None, host_id=None, workspace_clear=None):
        if not identifier(namespace):
            raise Refused('NAMESPACE')
        self.home, self.namespace = canonical_home(home), namespace
        self._transaction_lock = threading.RLock()
        self._typed_writer = None
        self._verify_submit_session = None
        self._typed_refs = set()
        self.ownership_contract, self.host_id, self.workspace_clear = ownership_contract, host_id, workspace_clear
        self.path = Path(self.home)/'plugin-data'/'symphony_pm'/digest(namespace)[:32]/'journal.sqlite3'
        if not self.path.resolve().is_relative_to(Path(self.home)):
            raise Refused('JOURNAL_PATH')
        make_user_dir(self.path.parent, mode=0o700, parents=True, exist_ok=True)
        with self._connection() as db:
            initialize(db, migrate_legacy=migrate_legacy, migrate_v1=migrate_v1)

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
    def _transaction(self, token=_BOOTSTRAP, *, authorize_commit=None):
        # A background launcher and the serving port share one local writer. Foreign writers still fail closed.
        with self._transaction_lock, self._connection() as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            try:
                if token is not _BOOTSTRAP:
                    self._authorize(db, token)
                yield db
                if authorize_commit is not None:
                    authorize_commit()
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

    def claim(self, identity: ServingIdentity, controller, expected_previous, *, proof=None, legacy_ownership=()):
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
            if proof is not None and (type(proof) is not PredecessorProof or not previous
                                      or proof.owner_digest != digest(previous) or proof.host_id != self.host_id
                                      or proof.kind != ('OWNER_DEAD' if previous['boot_id'] == identity.boot_id
                                                        else 'BOOT_CHANGED') or not proof.source):
                raise Refused('PREDECESSOR_PROOF_MISMATCH')
            if row and row['host_id'] is not None and self.host_id != row['host_id']:
                raise Refused('HOST_IDENTITY_CHANGED')
            if row and row['workspace_guarded'] and self.workspace_clear is None:
                raise Refused('WORKSPACE_GUARD_REQUIRED')
            if previous and previous['boot_id'] != identity.boot_id:
                if (type(proof) is not PredecessorProof or proof.kind != 'BOOT_CHANGED'
                        or proof.host_id != row['host_id'] or not proof.host_id):
                    raise Refused('HOST_BOOT_CHANGED_HOLD')
            writer = db.execute('SELECT epoch FROM writers WHERE namespace=?', (self.namespace,)).fetchone()
            if writer and (row is None or writer['epoch'] != row['epoch']):
                raise Refused('ORPHAN_WRITER')
            epoch = row['epoch']+1 if row else 1
            generation = row['observer_generation']+1 if row else 1
            self._recover(db, previous, identity.boot_id, proof, legacy_ownership)
            db.execute('INSERT OR REPLACE INTO serving_owner VALUES(?,?,?,?,?,0,?,?)',
                       (self.namespace, encode(asdict(identity)), controller, epoch, generation, self.host_id,
                        int(self.workspace_clear is not None)))
            db.execute('INSERT OR REPLACE INTO writers VALUES(?,?,?,?)',
                       (self.namespace, identity.incarnation, epoch, 0.0))
        return WriterToken(self.home, self.namespace, identity.incarnation, controller, epoch)

    def _recover(self, db, previous, current_boot, proof, legacy):
        # State and owner change together: a crash between them cannot repurpose a later owner's death.
        for row in db.execute('SELECT * FROM serving_launches WHERE namespace=? AND NOT COALESCE(' + _FREE + ',0)',
                              (self.namespace,)).fetchall():
            state, stop, source, hold = recovered_state(row, previous, current_boot, proof, legacy)
            if hold is None and self.workspace_clear is not None:
                hold = 'WORKSPACE_HELD'
            db.execute('UPDATE serving_launches SET state=?,stop_proof=?,proof_source=?,hold_reason=? WHERE attempt=?',
                       (state, stop, source, hold, row['attempt']))

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

    def restrict_permits(self, verify_submit_session):
        with self._transaction_lock:
            if self._typed_writer is not None:
                raise Refused('TYPED_WRITER_ALREADY_INSTALLED')
            self._typed_writer = object()
            self._verify_submit_session = verify_submit_session
            return self._typed_writer

    def launch_reserved(self, token, ref):
        with self._transaction(token) as db:
            row = db.execute('SELECT used FROM serving_permits WHERE ref=?', (ref,)).fetchone()
            return row is not None and row['used'] == 1

    def retire_typed_session(self, writer):
        with self._transaction_lock:
            if writer is None or writer is not self._typed_writer:
                raise Refused('TYPED_OWNER_DECISION_REQUIRED')
            self._typed_refs.clear()

    def has_approved_request(self, token, project, request_id):
        with self._transaction(token) as db:
            return db.execute("SELECT 1 FROM serving_permits WHERE json_extract(body,'$.project_ref')=? "
                              "AND json_extract(body,'$.identity.request_id')=? LIMIT 1",
                              (project, request_id)).fetchone() is not None

    def record_permit(self, token, permit: Permit, approval_id, approval_digest, now, *, writer=None,
                      authorize_commit=None):
        permit.validate(now)
        if (permit.identity.profile_home != self.home or permit.identity.namespace != self.namespace
                or not approval_id or not approval_digest):
            raise Refused('PERMIT_SCOPE')
        body = encode(asdict(permit))
        with self._transaction(token, authorize_commit=authorize_commit) as db:
            if (writer is not self._typed_writer or (writer is not None and not callable(authorize_commit))
                    or (getattr(permit.plan, 'typed_details', '') and writer is None)):
                raise Refused('TYPED_OWNER_DECISION_REQUIRED')
            old = db.execute('SELECT * FROM serving_permits WHERE ref=?', (permit.ref,)).fetchone()
            if old:
                if (old['body'], old['approval_id'], old['approval_digest']) != (body, approval_id, approval_digest):
                    raise Refused('PERMIT_CONFLICT')
                return
            db.execute('INSERT INTO serving_permits VALUES(?,?,?,?,?,?,0)',
                       (permit.ref, body, permit.binding_digest, digest(asdict(permit.identity)), approval_id, approval_digest))
        if writer is not None:
            self._typed_refs.add(permit.ref)

    def _current_permit(self, permit):
        if self._typed_writer is not None or getattr(permit.plan, 'typed_details', ''):
            return permit.ref in self._typed_refs
        return True

    def find_permit(self, token, identity, now):
        with self._transaction(token) as db:
            # Identity is host-created; no grant is minted by a matching model payload.
            found = []
            for row in db.execute('SELECT body FROM serving_permits WHERE identity_binding=?',
                                  (digest(asdict(identity)),)):
                permit = permit_from_json(json.loads(row['body']))
                if permit.identity == identity and self._current_permit(permit):
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
        if not self._current_permit(permit):
            raise Refused('TYPED_OWNER_DECISION_REQUIRED')
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
        def authorize_commit():
            self._verify_submit_session(permit.identity)
        with self._transaction(token, authorize_commit=authorize_commit if self._verify_submit_session else None) as db:
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
            if db.execute('SELECT 1 FROM serving_launches WHERE namespace=? AND NOT COALESCE(' + _FREE + ',0)',
                          (self.namespace,)).fetchone():
                raise Refused('UNSETTLED_PRIOR_LAUNCH_HOLD')
            owner_row = self._authorize(db, token)
            if owner_row['workspace_guarded'] and self.workspace_clear is None:
                raise Refused('WORKSPACE_GUARD_REQUIRED')
            if self.workspace_clear is not None and not self.workspace_clear():
                raise Refused('WORKSPACE_HELD')
            if db.execute("SELECT 1 FROM serving_launches WHERE namespace=? AND hold_reason='WORKSPACE_HELD'",
                          (self.namespace,)).fetchone():
                raise Refused('WORKSPACE_HELD')
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
            owner = json.loads(owner_row['identity'])
            db.execute("INSERT INTO serving_launches(namespace,project,job,attempt,ticket,state,owner_identity,"
                       "ownership_contract,host_id,boot_id) VALUES(?,?,?,?,?,'LAUNCH_INTENT',?,?,?,?)",
                       (self.namespace, request.project_ref, job, attempt, encode(asdict(ticket)),
                        digest(owner), self.ownership_contract, self.host_id, owner['boot_id']))
            db.execute('UPDATE serving_permits SET used=used+1 WHERE ref=?', (permit.ref,))
            return ticket, 'LAUNCH_INTENT', True

    def _launch(self, db, project, job, revision):
        row = db.execute('SELECT l.*,j.revision FROM serving_launches l JOIN jobs j USING(namespace,project,job) '
                         'WHERE l.namespace=? AND l.project=? AND l.job=?',
                         (self.namespace, project, job)).fetchone()
        if row is None or row['revision'] != revision:
            raise Refused('JOB_IDENTITY')
        ticket = ticket_from_json(json.loads(row['ticket']))
        if ((ticket.namespace, ticket.project, ticket.job, ticket.revision, ticket.attempt)
                != (self.namespace, project, job, revision, row['attempt'])):
            raise Refused('JOB_IDENTITY')
        return ticket, dict(row)

    def read_launch(self, token, project, job, revision):
        with self._transaction(token) as db:
            return self._launch(db, project, job, revision)

    def control_origin(self, token, request):
        """The consumed submit permit identifies the conversation even after its launch window."""
        with self._transaction(token) as db:
            ticket, row = self._launch(db, request.project_ref, request.job_ref, request.expected_job_revision)
            source = db.execute('SELECT * FROM serving_permits WHERE ref=?', (ticket.permit_ref,)).fetchone()
            if source is None:
                raise Refused('CONTROL_ORIGIN')
            permit = permit_from_json(json.loads(source['body']))
            if (source['used'] != 1 or permit.binding_digest != source['binding']
                    or permit.operation != 'submit_intent' or permit.identity.profile_home != self.home
                    or permit.identity.namespace != self.namespace or permit.identity.session_id != ticket.session
                    or permit.project_ref != ticket.project or permit.request_digest != ticket.request_digest
                    or permit.ref != ticket.permit_ref or permit.plan != ticket.plan):
                raise Refused('CONTROL_ORIGIN')
            return ticket, row, permit

    def stage_records(self, token, ticket):
        with self._transaction(token) as db:
            last = db.execute("SELECT seq,body FROM serving_observations WHERE attempt=? AND kind='CAPTURE' "
                              "ORDER BY seq DESC LIMIT 1", (ticket.attempt,)).fetchone()
            if last is None:
                return {}, {}
            capture = json.loads(last['body'])
            check = db.execute("SELECT body FROM serving_observations WHERE attempt=? AND kind='CHECK' "
                               "ORDER BY seq DESC LIMIT 1", (ticket.attempt,)).fetchone()
            if check:
                observation = json.loads(check['body'])
                if observation['capture_ref']['seq'] == last['seq']:
                    self._verify_check_capture(db, ticket, observation)
                    return capture, observation['check']
            return capture, {}

    def save_status(self, token, ticket, body, seq):
        with self._transaction(token) as db:
            if self._launch(db, ticket.project, ticket.job, ticket.revision)[0] != ticket:
                raise Refused('OBSERVATION_TARGET')
            self._append(db, ticket, body, seq)

    def cancelled_before_create(self, token, ticket):
        with self._transaction(token) as db:
            row = db.execute("SELECT body FROM serving_observations WHERE attempt=? AND kind='NOT_STARTED' "
                             "ORDER BY seq DESC LIMIT 1", (ticket.attempt,)).fetchone()
            return bool(row and json.loads(row['body']).get('reason') == 'PM_CANCEL')

    def authorize_control(self, token, request, context, now):
        with self._transaction(token) as db:
            ticket, row = self._launch(db, request.project_ref, request.job_ref, request.expected_job_revision)
            body = None
            if context.grant_ref.startswith('status:') and context.context_ref == context.grant_ref:
                parts = context.grant_ref.split(':')
                if len(parts) != 3 or not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) > 18:
                    raise Refused('CONTROL_TARGET_BINDING')
                saved = db.execute("SELECT body FROM serving_observations WHERE attempt=? AND seq=? AND kind='STATUS'",
                                   (ticket.attempt, int(parts[1]))).fetchone()
                body = json.loads(saved['body']) if saved else {}
                obs = body.get('observation', {})
                if (body.get('request_digest') != request.request_digest or obs.get('evidence_ref') != context.grant_ref
                        or obs.get('scheduler_epoch') != token.epoch or obs.get('observed_at') != context.now
                        or not 0 <= now-context.now < 30 or context.expires_at != context.now+30):
                    raise Refused('CONTROL_TARGET_BINDING')
                binding = digest([request.operation, request.payload_digest, ticket.permit_ref, ticket.session])
            else:
                permit, _ = self._permit(db, request, context, now)
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
            return ticket, row, body

    def unfinished(self, token):
        with self._transaction(token) as db:
            return [dict(r) for r in db.execute('SELECT * FROM serving_launches WHERE namespace=? AND NOT COALESCE('
                                                + _FREE + ',0)', (self.namespace,))]

    def unsettled_workspaces(self, token):
        with self._transaction(token) as db:
            return [ticket_from_json(json.loads(r[0])) for r in db.execute(
                "SELECT ticket FROM serving_launches WHERE namespace=? AND hold_reason='WORKSPACE_HELD' AND " + _FREE,
                (self.namespace,))]

    def workspace_settled(self, token, ticket):
        with self._transaction(token) as db:
            db.execute("UPDATE serving_launches SET hold_reason=NULL WHERE namespace=? AND attempt=? "
                       "AND hold_reason='WORKSPACE_HELD' AND " + _FREE, (self.namespace, ticket.attempt))

    def mark_not_started(self, token, ticket, *, reason=None, preparing=False, authorize_commit=None):
        """The losing side of the create race cannot clear a spent slot."""
        with self._transaction(token, authorize_commit=authorize_commit) as db:
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or row['state'] != 'LAUNCH_INTENT' or row['binding'] is not None:
                return False
            hold = 'WORKSPACE_HELD' if self.workspace_clear and (preparing or not self.workspace_clear()) else None
            db.execute("UPDATE serving_launches SET state='NOT_STARTED',hold_reason=? WHERE attempt=?",
                       (hold, ticket.attempt))
            if reason:
                self._append(db, ticket, {'kind': 'NOT_STARTED', 'reason': reason}, None)
            return True

    def release_by_owner(self, token, ticket, reason):
        with self._transaction(token) as db:
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or row['state'] != 'HELD_UNKNOWN' or not isinstance(reason, str) or not reason.strip():
                raise Refused('RELEASE_REQUIRES_OWNER_REASON')
            db.execute("UPDATE serving_launches SET state='RELEASED_BY_OWNER',hold_reason=NULL,proof_source=? "
                       "WHERE attempt=?", (reason, ticket.attempt))

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

    def dispatch_resume(self, token, ticket, generation, clock, action):
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
            self._valid_for_launch(permit_from_json(json.loads(permit_row['body'])), clock())
            return action()

    @staticmethod
    def _valid_for_launch(permit, now):
        if finite(now) and finite(permit.expires_at) and now >= permit.expires_at:
            raise LaunchExpired('PERMIT_EXPIRED_BEFORE_RESUME')
        permit.validate(now)

    def suspended_expired(self, token, ticket, binding):
        """Only after closing the exact suspended receiver whose resume action was never entered."""
        with self._transaction(token) as db:
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            if (actual != ticket or row['state'] not in ('BOUND', 'RESUME_INTENT')
                    or row['binding'] != encode(asdict(binding)) or binding.ticket != ticket):
                raise Refused('SUSPENDED_BINDING_CHANGED')
            hold = 'WORKSPACE_HELD' if self.workspace_clear and not self.workspace_clear() else None
            db.execute("UPDATE serving_launches SET state='NOT_STARTED',hold_reason=? WHERE attempt=?",
                       (hold, ticket.attempt))
            self._append(db, ticket, {'kind': 'NOT_STARTED', 'reason': 'PERMIT_EXPIRED_BEFORE_RESUME'}, None)

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
            self._valid_for_launch(permit, now)
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

    def cancel_causes(self, token, binding):
        with self._transaction(token) as db:
            return tuple(row[0] for row in db.execute('SELECT DISTINCT cause FROM serving_cancel_causes '
                         'WHERE effect_key=? ORDER BY cause', (binding.effect_key,)))

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

    def finish_job_stop(self, token, ticket, generation, *, stopped):
        """Bind the terminal to the current writer's saved readback, keeping the workspace held."""
        with self._transaction(token) as db:
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            if (actual != ticket or row['state'] not in ('CREATE_INTENT', 'BOUND', 'RESUME_INTENT')
                    or row['owner_identity'] != digest(json.loads(self._authorize(db, token)['identity']))):
                raise Refused('STOP_TARGET_BINDING')
            state, proof, source, hold = 'HELD_UNKNOWN', None, None, 'JOB_STOP_UNPROVEN'
            if stopped:
                self._healthy(db, token)
                binding = binding_from_json(json.loads(row['binding']))
                readback = db.execute('SELECT body FROM serving_readbacks WHERE effect_key=? AND writer_epoch=? '
                                      'AND observer_generation=?', (binding.effect_key, token.epoch, generation)).fetchone()
                if not readback or generation != self._authorize(db, token)['observer_generation']:
                    raise Refused('STOP_READBACK_UNKNOWN')
                body = json.loads(readback['body'])
                if (body['binding'] != json.loads(row['binding']) or body['job_identity_verified'] is not True
                        or body['job_active'] != 0 or body['job_members'] != [] or not body['processes']
                        or not all(item['signaled'] is True for item in body['processes'])):
                    raise Refused('STOP_READBACK_UNKNOWN')
                state, proof, source, hold = 'STOPPED', 'JOB_EMPTY', 'job-readback:'+digest(body), 'WORKSPACE_HELD'
            db.execute('UPDATE serving_launches SET state=?,stop_proof=?,proof_source=?,hold_reason=? WHERE attempt=?',
                       (state, proof, source, hold, ticket.attempt))

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

    def record_result(self, token, ticket, result, *, authorize_commit=None):
        body = encode(result)
        with self._transaction(token, authorize_commit=authorize_commit) as db:
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or row['binding'] is None:
                raise Refused('RESULT_TARGET_BINDING')
            if row['result'] is not None and row['result'] != body:
                raise Refused('RESULT_CONFLICT')
            if row['result'] is not None:
                return
            db.execute('UPDATE serving_launches SET result=? WHERE namespace=? AND project=? AND job=?',
                       (body, self.namespace, ticket.project, ticket.job))
            workspace = result.get('workspace') if isinstance(result, dict) else None
            if isinstance(workspace, dict) and row['state'] != 'HELD_UNKNOWN':
                if workspace.get('renamed') is False:
                    db.execute("UPDATE serving_launches SET hold_reason='WORKSPACE_HELD' WHERE attempt=?",
                               (ticket.attempt,))
                elif (workspace.get('renamed') is True and row['state'] == 'STOPPED'
                      and row['stop_proof'] in PROVEN_STOP and row['proof_source']
                      and self.workspace_clear is not None and self.workspace_clear() is True):
                    db.execute("UPDATE serving_launches SET hold_reason=NULL WHERE attempt=? "
                               "AND hold_reason='WORKSPACE_HELD'", (ticket.attempt,))
            if isinstance(result, dict):
                self._record_observations(db, ticket, result)

    def _record_observations(self, db, ticket, result):
        if result.get('exit_code') is not None:
            self._append(db, ticket, {'kind': 'EXIT', 'exit_code': result['exit_code']}, None)
        capture_seq = None
        if result.get('patch') is not None:
            capture_seq = self._append(db, ticket, {'kind': 'CAPTURE', 'source_sha': ticket.plan.source_sha,
                                                   'patch': result['patch']}, None)
        check = result.get('check')
        if check is not None:
            ref = {'attempt': ticket.attempt, 'seq': capture_seq,
                   'sha256': check.get('patch_sha256') if isinstance(check, dict) else None}
            self._append(db, ticket, {'kind': 'CHECK', 'capture_ref': ref, 'check': check}, None)

    @staticmethod
    def _verify_check_capture(db, ticket, observation):
        """A CHECK can only refer to an existing CAPTURE of these exact patch bytes."""
        ref = observation.get('capture_ref')
        check = observation.get('check')
        if (not isinstance(ref, dict) or not isinstance(check, dict)
                or ref.get('attempt') != ticket.attempt or check.get('attempt') != ticket.attempt
                or check.get('source_sha') != ticket.plan.source_sha
                or not sha(ref.get('sha256'), 64) or check.get('patch_sha256') != ref['sha256']
                or type(ref.get('seq')) is not int or not 0 < ref['seq'] < 2**63):
            raise Refused('CHECK_CAPTURE_BINDING')
        capture = db.execute("SELECT body FROM serving_observations WHERE attempt=? AND seq=? AND kind='CAPTURE'",
                             (ticket.attempt, ref['seq'])).fetchone()
        body = json.loads(capture[0]) if capture else {}
        patch = body.get('patch')
        if (not isinstance(patch, dict) or patch.get('sha256') != ref['sha256']
                or body.get('source_sha', ticket.plan.source_sha) != ticket.plan.source_sha):
            raise Refused('CHECK_CAPTURE_BINDING')

    @staticmethod
    def _next_seq(db, ticket):
        return db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM serving_observations WHERE attempt=?',
                          (ticket.attempt,)).fetchone()[0]

    def next_observation_seq(self, token, ticket):
        with self._transaction(token) as db:
            if self._launch(db, ticket.project, ticket.job, ticket.revision)[0] != ticket:
                raise Refused('OBSERVATION_TARGET')
            return self._next_seq(db, ticket)

    def _append(self, db, ticket, observation, expected_seq):
        if observation['kind'] == 'CHECK':
            self._verify_check_capture(db, ticket, observation)
        seq = self._next_seq(db, ticket)
        if expected_seq is not None and expected_seq != seq:
            raise Refused('OBSERVATION_SEQUENCE_CHANGED')
        db.execute('INSERT INTO serving_observations VALUES(?,?,?,?)',
                   (ticket.attempt, seq, observation['kind'], encode(observation)))
        return seq

    def append_observation(self, token, ticket, observation, *, expected_seq=None):
        if type(observation) is not dict or observation.get('kind') not in ('EXIT', 'CAPTURE', 'CHECK', 'APPLY'):
            raise Refused('OBSERVATION_KIND')
        with self._transaction(token) as db:
            if self._launch(db, ticket.project, ticket.job, ticket.revision)[0] != ticket:
                raise Refused('OBSERVATION_TARGET')
            return self._append(db, ticket, observation, expected_seq)
