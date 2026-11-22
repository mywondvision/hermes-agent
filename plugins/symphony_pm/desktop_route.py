"""A connected Desktop client may settle one conversation's plan at its Linux owner."""
import asyncio
import base64
import json
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from gateway.session_authorities import all_authorities
from hermes_cli.plugin_request_binding import canonical_home
from hermes_cli.plugins_loader import _plugin_home_scope
from .desktop_approval import PLAN_EVENT, current_session, decide_presented, pending_plan
from .transport.types import Refused, digest
from .typed_approval import _owner_route


def signing_bytes(fields):
    return json.dumps(fields, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


@dataclass(frozen=True)
class Registration:
    client_id: str
    connection_id: str
    public_key: str
    install_id: str
    boot_id: str
    host_fingerprint: str
    profile: str
    namespace: str
    operator_id: str
    conversation_id: str
    registration_id: str


class PlanRoute:
    def __init__(self, approval):
        self.approval = approval

    @property
    def forward_profile(self):
        return self.approval.scope.profile_home

    def forward_binding(self, authority, session_id, *, routing_lookup=None):
        """Classify only an explicitly registered conversation. Never infer ordinary from absence."""
        approval = self.approval
        reg = self.registration()
        with _plugin_home_scope(Path(reg.profile)):
            modes = approval.ctx.get_config('desktop_forward_modes', {})
            if not isinstance(modes, dict):
                raise PermissionError()
            mode = modes.get(session_id)
            owner = approval.owner
            owner._check(owner.token)
            if session_id == reg.conversation_id and mode in ('shared', 'personal'):
                if mode == 'personal' and not self.operator_registrations():
                    raise PermissionError()
                route = _owner_route(approval.ctx)
                if (route['platform'], route['user_id'], route['chat_id']) != (
                        'local', reg.operator_id, session_id):
                    raise PermissionError()
                session = current_session(authority.runner, approval.scope, route, routing_lookup=routing_lookup)
                kind = 'forward'
            elif mode == 'ordinary' and session_id != reg.conversation_id:
                session, kind = session_id, 'ordinary'
            else:
                raise PermissionError()
        return kind, digest([asdict(reg), owner.token.incarnation, owner.token.epoch, owner.generation,
                             authority.instance_id, authority.epoch, session, session_id, kind, mode]), mode

    def operator_registrations(self):
        """The profile administrator supplies these public-key grants in local config."""
        with _plugin_home_scope(Path(self.forward_profile)):
            value = self.approval.ctx.get_config('desktop_operator_registrations', {})
        return value if isinstance(value, dict) else {}

    def registration(self):
        scope = self.approval.scope
        with _plugin_home_scope(Path(scope.profile_home)):
            value = self.approval.ctx.get_config('desktop_plan_route', None)
        if not scope.active or type(value) is not dict or set(value) != set(Registration.__dataclass_fields__):
            raise PermissionError()
        if not all(type(v) is str and 0 < len(v) <= 2048 for v in value.values()):
            raise PermissionError()
        reg = Registration(**value)
        owner = self.approval.owner
        if (reg.profile != scope.profile_home or reg.namespace != scope.namespace
                or reg.install_id != owner.launcher.qualification.installation_id
                or reg.boot_id != owner.guard.identity.boot_id):
            raise PermissionError()
        return reg

    def matches(self, runner, params):
        try:
            reg = self.registration()
        except PermissionError:
            return False
        return (params == {'client_id': reg.client_id, 'connection_id': reg.connection_id}
                and any(canonical_home(a.profile_id) == reg.profile for a in all_authorities(runner)))

    def connect(self, runner, send):
        return PlanConnection(self, runner, send)


def register_route(approval):
    from gateway.session_scoped_ws import register
    register(PlanRoute(approval), approval.scope)


class PlanConnection:
    def __init__(self, route, runner, send):
        self.route, self.runner, self.send = route, runner, send
        self.approval = route.approval
        self.reg = route.registration()
        self.transport_id = secrets.token_hex(24)
        self.challenge = self.stream = None
        self.loop = asyncio.get_running_loop()
        self.closed = False
        self.commit_lock = RLock()

    def _binding(self):
        approval, reg = self.approval, self.reg
        if self.closed or self.route.registration() != reg:
            raise PermissionError()
        operator = _owner_route(approval.ctx)
        if (operator['platform'], operator['user_id'], operator['chat_id']) != (
                'local', reg.operator_id, reg.conversation_id):
            raise PermissionError()
        session = current_session(self.runner, approval.scope, operator)
        authority, = [a for a in all_authorities(self.runner) if canonical_home(a.profile_id) == reg.profile]
        live = authority.sessions[reg.conversation_id]
        owner = approval.owner
        owner._check(owner.token)
        generation = f'{owner.token.incarnation}:{owner.token.epoch}:{owner.generation}'
        return session, generation, live.event_stream

    def _require(self):
        try:
            session, generation, stream = self._binding()
        except (Refused, KeyError, PermissionError, ValueError):
            self.close()
            raise PermissionError() from None
        if self.stream is None:
            raise PermissionError()
        if (stream is not self.stream or self._notice not in stream.private_subscribers.get(PLAN_EVENT, ())
                or (session, generation) != (self.session, self.generation)):
            self.close()
            raise PermissionError()

    def _notice(self):
        self.loop.call_soon_threadsafe(self._queue_notice)

    def _queue_notice(self):
        if not self.closed:
            self.loop.create_task(self._notify())

    async def _notify(self):
        try:
            with _plugin_home_scope(Path(self.reg.profile)):
                self._require()
            await self.send(dict(jsonrpc='2.0', method='event', params=dict(
                type=PLAN_EVENT, session_id=self.reg.conversation_id, payload={})))
        except (PermissionError, Refused, OSError, RuntimeError):
            self.close()

    def close(self):
        with self.commit_lock:
            self.closed = True
            self.challenge = None
            if self.stream is not None:
                with self.stream.lock:
                    self.stream.private_subscribers[PLAN_EVENT].discard(self._notice)
                self.stream = None

    @contextmanager
    def _commit_guard(self):
        with self.commit_lock:
            if self.closed:
                raise PermissionError()
            yield

    def _issue(self, params):
        r = self.reg
        if self.challenge or self.stream or params != {'client_id': r.client_id, 'connection_id': r.connection_id}:
            raise PermissionError()
        self.session, self.generation, stream = self._binding()
        fields = ['symphony-plan', '1', secrets.token_urlsafe(32), r.install_id, r.host_fingerprint,
                  r.profile, r.namespace, r.operator_id, r.conversation_id, self.session, self.transport_id,
                  self.generation, r.connection_id, r.client_id]
        self.challenge = (time.monotonic() + 60, signing_bytes(fields), stream)
        return {'fields': fields}

    def _authenticate(self, params):
        issued, self.challenge = self.challenge, None
        if not issued or issued[0] <= time.monotonic() or set(params) != {'signature'}:
            raise PermissionError()
        try:
            signature = base64.b64decode(params['signature'], validate=True)
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(self.reg.public_key, validate=True))
            key.verify(signature, issued[1])
        except (InvalidSignature, TypeError, ValueError) as exc:
            raise PermissionError() from exc
        session, generation, stream = self._binding()
        if (session, generation, stream) != (self.session, self.generation, issued[2]):
            raise PermissionError()
        with stream.lock:
            stream.private_subscribers.setdefault(PLAN_EVENT, set()).add(self._notice)
            self.stream = stream
        return dict(state='READY', transport_id=self.transport_id, session_id=session,
                    conversation_id=self.reg.conversation_id)

    def _command(self, params):
        self._require()
        arg = params.get('arg')
        if type(arg) is not str or params != dict(name='symphony', session_id=self.reg.conversation_id,
                                                 interaction='displayed-plan', arg=arg):
            raise PermissionError()
        verb, space, body = arg.partition(' ')
        if arg == 'status':
            try:
                pending = pending_plan(self.approval, self.session)
            except Refused as exc:
                return {'output': str(exc)}
            self._require()
            return {'output': json.dumps({'pending_plan': pending})}
        if not space or verb not in {'plan-presented', 'plan-answer', 'plan-receipt', 'plan-result'}:
            raise PermissionError()
        # The regular launch reservation rechecks the gateway's conversation context.
        # Bind the registered owner's live route; the request cannot supply these attributes.
        from gateway.session_context import clear_session_vars, set_session_vars
        route = _owner_route(self.approval.ctx)
        tokens = set_session_vars(platform=route['platform'], user_id=route['user_id'], chat_id=route['chat_id'],
                                  chat_type='dm', session_key=route['session_key'], session_id=self.session)
        try:
            result = decide_presented(self.approval, self.session, json.loads(body), viewer=self.transport_id,
                                      operation=verb[5:], check_viewer=self._require, commit_guard=self._commit_guard)
        except Refused as exc:
            return {'output': str(exc)}
        finally:
            clear_session_vars(tokens)
        self._require()
        return {'output': json.dumps(result, ensure_ascii=False)}

    async def dispatch(self, method, params):
        from hermes_cli.plugin_messages import _gateway_loop
        if self.closed or _gateway_loop()[0] is not self.runner:
            raise PermissionError()
        with _plugin_home_scope(Path(self.reg.profile)):
            try:
                if method == 'route.challenge':
                    return self._issue(params)
                if method == 'route.authenticate':
                    return self._authenticate(params)
                if method == 'command.dispatch':
                    return await asyncio.to_thread(self._command, params)
            except (Refused, KeyError) as exc:
                raise PermissionError() from exc
        raise PermissionError()
