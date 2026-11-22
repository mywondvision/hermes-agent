"""Challenge and proof verification for the owner-side Desktop OPERATOR IPC."""

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from threading import RLock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from gateway.owner_identity import host_owner_subject
from gateway.session_authorities import all_authorities, owner_scope
from gateway.session_contract import Principal, SessionRef, Submission
from gateway.session_desktop_operator_store import (
    admit, binding_key, check_binding, deny, read_record, routing_index, routing_session_id, write_record,
)
from gateway.session_forward import _binding
from gateway.session_scoped_ws import forward_provider
from hermes_state_runtime import RuntimeStoreError, get_session_admission


_BOUND_DIGEST_FIELDS = {
    'stop_reconcile': ('attempt_id', 'command_id', 'reason'),
    'stop_replace': ('attempt_id', 'previous_command_id', 'command_id', 'reason'),
    'apply_info': ('attempt_id',),
    'apply': ('attempt_id', 'job', 'capture_seq', 'check_seq', 'patch_sha256', 'command_id'),
    'archive': ('attempt_id', 'job', 'capture_seq', 'check_seq', 'patch_sha256', 'command_id'),
    'apply_result': ('attempt_id', 'command_id'),
}
_FIELDS = {
    'submit': {'text'}, 'receipt': set(), 'recover': set(), 'status': {'attempt_id'},
    'stop': {'attempt_id', 'command_id', 'reason'}, 'stop_result': {'attempt_id', 'command_id'},
    **{action: set(fields) for action, fields in _BOUND_DIGEST_FIELDS.items()},
}
_ACTION_RIGHTS = {'recover': 'receipt', 'stop_reconcile': 'stop', 'stop_replace': 'stop', 'apply_info': 'receipt'}
_RESULT_EFFECTS = frozenset({'apply', 'archive'})
_SEQUENCES = frozenset({'capture_seq', 'check_seq'})
_CHALLENGE = {'action', 'submission_id', 'installation_key', 'window_key', 'window_generation', 'payload_sha256'}
_EXECUTE = {'challenge_id', 'installation_signature', 'window_signature', 'payload'}
_CONTEXT = {'profile_id', 'session_id', 'instance_id', 'transport_id'}
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
_HASH = re.compile(r'[0-9a-f]{64}')
_TTL_MS = 30_000
_MAX_PENDING = 1024


def signing_bytes(fields):
    return json.dumps(fields, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def payload_digest(payload, *, action=None):
    if action in _BOUND_DIGEST_FIELDS:
        fields = [action, *(payload[key] for key in _BOUND_DIGEST_FIELDS[action])]
    else:
        fields = [payload.get(key) for key in ('text', 'attempt_id', 'command_id', 'reason')]
    return hashlib.sha256(signing_bytes(fields)).hexdigest()


def binary(value, size):
    if not isinstance(value, str) or len(value) != ((size + 2) // 3) * 4:
        deny()
    decoded = base64.b64decode(value, validate=True)
    if len(decoded) != size or base64.b64encode(decoded).decode('ascii') != value:
        deny()
    return decoded


def now_ms():
    return time.time_ns() // 1_000_000


def registration(provider, installation, action):
    item = provider.operator_registrations().get(installation)
    allowed = {'submit', 'receipt', 'status', 'stop', 'stop_result', 'apply', 'archive', 'apply_result'}
    right = _ACTION_RIGHTS.get(action, action)
    if (type(item) is not dict or set(item) != {'operator_id', 'generation', 'expires_at_ms', 'rights'}
            or not isinstance(item['operator_id'], str) or not _ID.fullmatch(item['operator_id'])
            or type(item['generation']) is not int or item['generation'] < 1
            or type(item['expires_at_ms']) is not int or item['expires_at_ms'] <= now_ms()
            or type(item['rights']) is not list or any(type(value) is not str for value in item['rights'])
            or not set(item['rights']) <= allowed or right not in item['rights']):
        deny()
    return item['operator_id'], item['generation'], item['expires_at_ms'], frozenset(item['rights'])


def check_payload(action, payload):
    if (type(payload) is not dict or set(payload) != _FIELDS[action]
            or any(type(value) is not str or '\0' in value
                   for key, value in payload.items() if key not in _SEQUENCES)):
        deny()
    if action == 'submit' and (not payload['text'].strip() or len(payload['text'].encode('utf-8')) > 48000):
        deny()
    if any(not _ID.fullmatch(payload[key]) for key in ('attempt_id', 'command_id', 'previous_command_id', 'job')
           if key in payload):
        deny()
    if action == 'stop_replace' and payload['previous_command_id'] == payload['command_id']:
        deny()
    if 'reason' in payload and (not payload['reason'].strip() or len(payload['reason']) > 500):
        deny()
    if action in _RESULT_EFFECTS:
        capture, check = (payload[key] for key in ('capture_seq', 'check_seq'))
        if (type(capture) is not int or type(check) is not int or not 0 < capture < check < 2**53
                or not _HASH.fullmatch(payload['patch_sha256'])):
            deny()


@dataclass(frozen=True)
class Proof:
    fields: tuple
    grant: tuple

    @property
    def principal(self):
        return [self.grant[0], self.grant[1], self.fields[9], self.fields[10]]

    def verify(self, params):
        message = signing_bytes(self.fields)
        for key, field in ((9, 'installation_signature'), (10, 'window_signature')):
            public = Ed25519PublicKey.from_public_bytes(binary(self.fields[key], 32))
            public.verify(binary(params[field], 64), message)


class OperatorIPC:
    def __init__(self, runner):
        self.runner = runner
        self.proofs = {}
        self.stop_challenges = {}
        self.closed = {}
        self.active_effects = {}
        self.lock = RLock()

    def trim(self, now):
        self.proofs = {key: proof for key, proof in self.proofs.items() if proof.fields[-1] > now}
        self.closed = {key: until for key, until in self.closed.items() if until > now or key in self.active_effects}

    @contextmanager
    def effect_transport(self, transport):
        with self.lock:
            self.active_effects[transport] = self.active_effects.get(transport, 0) + 1
        try:
            yield
        finally:
            with self.lock:
                remaining = self.active_effects[transport] - 1
                if remaining:
                    self.active_effects[transport] = remaining
                else:
                    del self.active_effects[transport]

    @staticmethod
    def _owns_stop_challenge(authority, ref, proof):
        key = binding_key(ref.session_id, proof.fields[8])
        with authority.db._read_ctx() as conn:
            try:
                check_binding(read_record(conn, key), proof.principal, proof.fields[11], list(proof.fields[3:6]))
            except RuntimeStoreError:
                return False
        return True

    @staticmethod
    def _stop_challenge_key(proof, digest):
        return (*proof.fields[3:5], *proof.fields[6:9], *proof.principal, digest)

    def close(self, transport):
        with self.lock:
            now = now_ms()
            self.trim(now)
            self.closed[transport] = now + 2 * _TTL_MS
            self.proofs = {key: proof for key, proof in self.proofs.items() if proof.fields[5] != transport}

    def challenge(self, authority, ref, actor, provider, params):
        action, submission_id = params['action'], params['submission_id']
        generation = params['window_generation']
        if (action not in _FIELDS or not _ID.fullmatch(submission_id)
                or not _HASH.fullmatch(params['payload_sha256']) or type(generation) is not int
                or not 0 <= generation < 2**53 - 1):
            deny()
        installation, window = params['installation_key'], params['window_key']
        if binary(installation, 32) == binary(window, 32):
            deny()
        binding = _binding(authority, ref, actor)
        if binding['mode'] != 'personal' or binding['route'] != 'forward':
            deny()
        grant = registration(provider, installation, action)
        challenge_id, transport, now = secrets.token_hex(24), params['transport_id'], now_ms()
        expires = min(now + _TTL_MS, grant[2])
        fields = ('hermes.desktop.operator', 1, action, authority.instance_id, binding['generation'],
                  transport, ref.profile_id, ref.session_id, submission_id, installation, window, generation,
                  challenge_id, params['payload_sha256'], expires)
        proof = Proof(fields, grant)
        track_stop = action == 'stop' and self._owns_stop_challenge(authority, ref, proof)
        with self.lock:
            self.trim(now)
            if transport in self.closed or len(self.proofs) >= _MAX_PENDING:
                deny()
            self.proofs[challenge_id] = proof
            if track_stop and len(self.stop_challenges) < _MAX_PENDING:
                key = self._stop_challenge_key(proof, proof.fields[13])
                # Closed transports retain the witness; issuing another challenge cannot
                # restore a consumed witness or extend its lifetime.
                self.stop_challenges.setdefault(key, expires + _TTL_MS)
        return dict(version=1, challenge_id=challenge_id, ref=asdict(ref), window_generation=generation,
                    message=base64.b64encode(signing_bytes(fields)).decode('ascii'))

    async def execute(self, authority, ref, actor, provider, params):
        challenge_id, transport = params['challenge_id'], params['transport_id']
        with self.lock:
            self.trim(now_ms())
            proof = self.proofs.get(challenge_id)
            if (proof is None or proof.fields[5:8] != (transport, ref.profile_id, ref.session_id)
                    or proof.fields[3] != authority.instance_id):
                deny()
            del self.proofs[challenge_id]
            if proof.fields[2] == 'stop':
                key = self._stop_challenge_key(proof, proof.fields[13])
                if key in self.stop_challenges:
                    self.stop_challenges[key] = 0
        action = proof.fields[2]
        payload = params['payload']
        check_payload(action, payload)
        if proof.fields[13] != payload_digest(payload, action=action):
            deny()
        proof.verify(params)

        routing_scope, separate_db = await routing_index(authority)
        routed_session = None

        def routing_session(route, conn):
            nonlocal routed_session
            if conn is not None or separate_db is not None:
                routed_session = routing_session_id(conn, routing_scope, route, separate_db=separate_db)
            return routed_session

        def authorize(conn=None, *, committing=False, admitted=False):
            if (getattr(self.runner, '_draining', False) or transport in self.closed
                    or (not admitted and proof.fields[-1] <= now_ms())
                    or registration(provider, proof.fields[9], action) != proof.grant):
                deny()
            # The SessionStore writes routing rows while holding its own mutex. Read the same
            # row through our active SQLite transaction instead of reversing that lock order.
            lookup = None if conn is None and not committing else lambda route: routing_session(route, conn)
            binding = _binding(authority, ref, actor, routing_lookup=lookup)
            if binding['mode'] != 'personal' or binding['generation'] != proof.fields[4]:
                deny()

        @contextmanager
        def commit_guard(*, admitted=False, conn=None):
            with self.lock:
                authorize(conn, committing=True, admitted=admitted)
                if action == 'stop_reconcile':
                    key = self._stop_challenge_key(proof, payload_digest(payload))
                    if self.stop_challenges.get(key, 0) <= now_ms():
                        raise RuntimeStoreError('stop_reconcile_unproven')
                yield

        if action == 'submit':
            return await self.submit(authority, ref, actor, proof, payload, authorize, commit_guard)
        return await asyncio.to_thread(self.control, authority, ref, provider, proof, payload, authorize, commit_guard)

    async def submit(self, authority, ref, actor, proof, payload, authorize, commit_guard):
        submission_id, generation = proof.fields[8], proof.fields[11]
        connection = list(proof.fields[3:6])
        key = binding_key(ref.session_id, submission_id)
        request = Submission(submission_id, ref, payload, 'queue')

        def guard(conn, admission_id, *, replay):
            authorize(conn)
            admit(conn, key, proof.principal, generation, connection, admission_id,
                  ref.session_id, submission_id, replay=replay)

        receipt = await authority.submit(actor, request, _authorize_write=authorize,
                                         _desktop_binding=guard, _shared_forward=True, _commit_guard=commit_guard)
        authorize()
        return dict(ref=asdict(ref), submission_id=submission_id, admission_id=receipt.admission_id,
                    status=receipt.status)

    def control(self, authority, ref, provider, proof, payload, authorize, commit_guard):
        action, submission_id, generation = proof.fields[2], proof.fields[8], proof.fields[11]
        connection = list(proof.fields[3:6])
        key = binding_key(ref.session_id, submission_id)

        def current(conn, *, recover=False, admitted=False):
            authorize(conn, admitted=admitted)
            return check_binding(read_record(conn, key), proof.principal, generation, connection, recover=recover)

        def recover(conn):
            record = current(conn, recover=True)
            if record['connection'] == connection:
                deny()
            write_record(conn, key, dict(record, connection=connection, generation=generation + 1))

        if action == 'recover':
            authority.db._execute_write(recover, commit_guard=commit_guard)
            return dict(ref=asdict(ref), submission_id=submission_id, window_generation=generation + 1)
        with authority.db._read_ctx() as conn:
            admission_id = current(conn)['admission_id']
        row = get_session_admission(authority.db, admission_id=admission_id)
        if row is None or row['target_session_id'] != ref.session_id or row['request_id'] != submission_id:
            deny()
        from plugins.symphony_pm.desktop_operator import attempt_operation
        live_route = authority.sessions[ref.session_id].route
        is_effect = action in _RESULT_EFFECTS
        # Keep the closed transport witness until the worker exits, even if the RPC waiter is cancelled.
        with self.effect_transport(connection[-1]) if is_effect else nullcontext():
            result = attempt_operation(provider.approval, authority.db, ref, live_route, admission_id, proof, payload,
                                       current, commit_guard)
            with authority.db._read_ctx() as conn:
                current(conn, admitted=is_effect)
        if action == 'receipt':
            return dict(ref=asdict(ref), submission_id=submission_id, admission_id=admission_id,
                        status=row['status'], attempt=result)
        return result

    async def dispatch(self, verb, params, peer):
        fields = {'operator-challenge': _CHALLENGE, 'operator-execute': _EXECUTE, 'operator-close': set()}[verb]
        if (peer != host_owner_subject() or set(params) != _CONTEXT | fields
                or any(type(params[key]) is not str or not params[key] for key in _CONTEXT)
                or not _ID.fullmatch(params['transport_id'])):
            deny()
        if verb == 'operator-close':
            self.close(params['transport_id'])
            return {'closed': True}
        matches = [authority for authority in all_authorities(self.runner)
                   if authority.profile_id == params['profile_id'] and authority.instance_id == params['instance_id']]
        if len(matches) != 1:
            deny()
        authority, = matches
        actor = Principal(peer, authority.profile_id, frozenset({'session:submit'}), 'desktop-operator-forward')
        ref = SessionRef(authority.profile_id, params['session_id'])
        with owner_scope(authority):
            provider = forward_provider(authority)
            if verb == 'operator-challenge':
                return await asyncio.to_thread(self.challenge, authority, ref, actor, provider, params)
            return await self.execute(authority, ref, actor, provider, params)


def install_operator_verbs(server, runner, loop):
    from plugins.symphony_pm.transport.types import Refused

    operator = OperatorIPC(runner)

    def handler(verb):
        async def invoke(params, peer):
            try:
                return await operator.dispatch(verb, params, peer)
            except RuntimeStoreError as exc:
                return {'operator_error': 'admission_conflict' if exc.reason == 'admission_conflict' else 'OPERATOR_HOLD'}
            except (ValueError, PermissionError, InvalidSignature, Refused):
                return {'operator_error': 'OPERATOR_HOLD'}

        def call(params, peer):
            return asyncio.run_coroutine_threadsafe(invoke(params, peer), loop).result(timeout=20)
        return call

    for verb in ('operator-challenge', 'operator-execute', 'operator-close'):
        server.private_handlers[verb] = handler(verb)
