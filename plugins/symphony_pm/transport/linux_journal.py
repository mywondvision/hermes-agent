"""Linux reservation and terminal proof policy for an isolated v2 serving journal."""
from contextlib import contextmanager
from dataclasses import asdict
from functools import partial
import json
import time

from .journal import Journal, encode
from .linux_identity import CONTRACT, linux_binding_from_json
from .recovery import PredecessorProof
from .types import Refused, digest, finite

_SOURCE = 'linux-cgroup.events/1'
_UNKNOWN = ('HELD_UNKNOWN', None, None, 'CGROUP_STOP_UNPROVEN')
_COLUMNS = ('state', 'stop_proof', 'proof_source', 'binding', 'ticket', 'ownership_contract', 'host_id', 'boot_id',
            'namespace', 'project', 'job', 'attempt')


def _proves_empty(body, bound, required):
    if (body.get('source') != _SOURCE or body.get('binding') != bound or body.get('launch_closed') is not True
            or type(body.get('populated')) is not int or body['populated'] != 0
            or not finite(body.get('observed_at')) or not all(type(body.get(k)) is int and body[k] > 0
                                                               for k in ('epoch', 'generation'))):
        return False
    observations = body['processes']
    if not observations or not all(p['signaled'] is True for p in observations):
        return False
    if not all(type(p['identity']['pid']) is int and p['identity']['pid'] > 0
               and isinstance(p['identity']['creation'], str) and p['identity']['creation'].isascii()
               and p['identity']['creation'].isdigit() for p in observations):
        return False
    seen = {(p['identity']['pid'], p['identity']['creation']) for p in observations}
    return len(seen) == len(observations) and seen == required


def _terminal(db, state, kind, source, binding, ticket, contract, host, boot, namespace, project, job, attempt):
    if state != 'STOPPED' or kind != 'CGROUP_EMPTY' or contract != CONTRACT:
        return False
    try:
        bound, body = json.loads(binding), json.loads(source)
        decoded = linux_binding_from_json(bound)
        t = decoded.ticket
        if ((host, boot) != (decoded.host_id, decoded.boot_id) or bound['ticket'] != json.loads(ticket)
                or (namespace, project, job, attempt) != (t.namespace, t.project, t.job, t.attempt)):
            return False
        saved = db.execute('SELECT writer_epoch,observer_generation,observed_at,body FROM serving_readbacks '
                           'WHERE effect_key=?', (decoded.effect_key,)).fetchone()
        required = {tuple(r) for r in db.execute('SELECT pid,creation FROM serving_members WHERE attempt=?',
                                                 (attempt,))}
        cancel = db.execute('SELECT cancel_at FROM effects WHERE effect_key=?', (decoded.effect_key,)).fetchone()
        return (saved is not None and saved[3] == source and (saved[0], saved[1]) == (body['epoch'], body['generation'])
                and {(p.pid, p.creation) for p in decoded.known_processes} <= required
                and _proves_empty(body, bound, required) and finite(saved[2])
                and 0 <= saved[2] - body['observed_at'] <= 30
                and (not cancel or finite(cancel[0]) and cancel[0] <= body['observed_at']))
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, Refused):
        return False


def saved_stop(db, row):
    return _terminal(db, *(row[k] for k in _COLUMNS))


class LinuxJournal(Journal):
    free_predicate = ("((state='NOT_STARTED' AND binding IS NULL AND ownership_contract='"
                      + CONTRACT + "') OR linux_terminal(" + ','.join(_COLUMNS) + '))')
    decode_binding = staticmethod(linux_binding_from_json)
    clock = staticmethod(time.time)

    @contextmanager
    def _connection(self):
        with super()._connection() as db:
            db.create_function('linux_terminal', len(_COLUMNS), partial(_terminal, db))
            yield db

    primary_checks_only = True
    backend = CONTRACT

    def checked_apply_capture(self, ticket, captures):
        from .recapture import read_bound_patch
        primary = json.loads(captures[0]['body'])
        if len(captures) > 1:
            latest = json.loads(captures[-1]['body'])
            patch = latest['patch']
            if (latest.get('source_sha') != ticket.plan.source_sha or latest.get('baseline_matches_source') is not True
                    or any(patch.get(k) != primary['patch'].get(k) for k in ('sha256', 'paths'))):
                raise Refused('ROBOT_RECAPTURE_CHANGED')
            read_bound_patch(self.path.parent/'attempts'/digest(ticket.attempt)[:12], patch)
        return captures[0]['seq'], primary

    def proven_stop(self, row, *, db=None):
        if db is not None:
            return saved_stop(db, row)
        with self._connection() as reader:
            reader.execute('BEGIN')
            return saved_stop(reader, row)

    containment_empty = proven_stop

    @staticmethod
    def recover_state(row, previous, current_boot, proof, legacy):
        if (row['state'] == 'LAUNCH_INTENT' and row['binding'] is None and previous
                and type(proof) is PredecessorProof and proof.kind == 'OWNER_DEAD'
                and proof.contract == row['ownership_contract'] == CONTRACT and proof.source
                and proof.owner_digest == row['owner_identity'] == digest(previous)
                and row['boot_id'] == current_boot == previous['boot_id']
                and row['host_id'] == proof.host_id):
            return 'NOT_STARTED', None, proof.source, None
        return _UNKNOWN

    def settle_git_check(self, token, invocation, terminal):
        from .linux_robot_check import settled_record
        with self._transaction(token) as db:
            rows = db.execute(
                "SELECT DISTINCT l.project,l.job,j.revision FROM serving_observations o "
                "JOIN serving_launches l USING(attempt) JOIN jobs j USING(namespace,project,job) "
                "WHERE l.namespace=? AND o.kind='APPLY' AND json_extract(o.body,'$.git_invocation')=?",
                (self.namespace, invocation.key)).fetchall()
            if len(rows) != 1:
                raise Refused('CHECK_INTENT_CHANGED')
            ticket, _ = self._launch(db, *rows[0])
            if digest(asdict(ticket)) != invocation.ticket_sha256:
                raise Refused('CHECK_INTENT_CHANGED')
            latest = db.execute("SELECT body FROM serving_observations WHERE attempt=? AND kind='APPLY' "
                                "ORDER BY seq DESC LIMIT 1", (ticket.attempt,)).fetchone()
            record = json.loads(latest[0])
            if record['state'] not in ('CHECKING', 'CHECK_INCOMPLETE', 'CHECK_RECOVERED', 'CHECK_ABORTED'):
                raise Refused('CHECK_INTENT_CHANGED')
            observed = settled_record(record, invocation, terminal)
            if observed != record:
                self._append(db, ticket, observed, self._next_seq(db, ticket))

    check_slot = None

    def _require_reservation_clear(self):
        super()._require_reservation_clear()
        if self.check_slot is not None:
            self.check_slot()

    def _append(self, db, ticket, observation, expected_seq):
        if observation['kind'] in ('CAPTURE', 'CHECK', 'APPLY'):
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            if actual != ticket or not self.proven_stop(row, db=db) or row['hold_reason']:
                raise Refused('CAPTURE_REQUIRES_SETTLED_CGROUP')
        return super()._append(db, ticket, observation, expected_seq)

    def release_by_owner(self, token, ticket, reason):
        with self._transaction(token):
            raise Refused('LINUX_RELEASE_REQUIRES_STOP_PROOF')

    def check_resume_effects(self, db, row):
        binding = self.decode_binding(json.loads(row['binding']))
        if db.execute('SELECT 1 FROM serving_cancel_causes WHERE effect_key=?',
                      (binding.effect_key,)).fetchone():
            raise Refused('RESUME_AFTER_CANCEL_WITHHELD')

    def bind_native(self, token, ticket, binding):
        with self._transaction(token) as db:
            owner = self._authorize(db, token)
            if (owner['host_id'] != binding.host_id or json.loads(owner['identity'])['boot_id'] != binding.boot_id
                    or self.ownership_contract != CONTRACT):
                raise Refused('LINUX_BINDING_SCOPE')
        super().bind_native(token, ticket, binding)

    def finish_job_stop(self, token, ticket, generation, *, stopped):
        with self._transaction(token) as db:
            actual, row = self._launch(db, ticket.project, ticket.job, ticket.revision)
            owner = self._authorize(db, token)
            if (actual != ticket or row['state'] not in ('CREATE_INTENT', 'BOUND', 'RESUME_INTENT', 'HELD_UNKNOWN')
                    or row['ownership_contract'] != CONTRACT or row['host_id'] != owner['host_id']
                    or row['boot_id'] != json.loads(owner['identity'])['boot_id']):
                raise Refused('STOP_TARGET_BINDING')
            state, proof, source, hold = _UNKNOWN
            if stopped:
                if self._healthy(db, token) != generation or not row['binding']:
                    raise Refused('STOP_READBACK_UNKNOWN')
                bound = self.decode_binding(json.loads(row['binding']))
                observation = db.execute('SELECT body FROM serving_readbacks WHERE effect_key=? AND writer_epoch=? '
                                         'AND observer_generation=?', (bound.effect_key, token.epoch, generation)).fetchone()
                if not observation:
                    raise Refused('STOP_READBACK_UNKNOWN')
                source = observation[0]
                candidate = {**row, 'state': 'STOPPED', 'stop_proof': 'CGROUP_EMPTY', 'proof_source': source}
                if (not saved_stop(db, candidate) or not 0 <= self.clock()-json.loads(source)['observed_at'] <= 30):
                    raise Refused('STOP_READBACK_UNKNOWN')
                state, proof, hold = 'STOPPED', 'CGROUP_EMPTY', 'WORKSPACE_HELD'
            db.execute('UPDATE serving_launches SET state=?,stop_proof=?,proof_source=?,hold_reason=? WHERE attempt=?',
                       (state, proof, source, hold, ticket.attempt))
