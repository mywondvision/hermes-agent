"""One visible plan and a typed decision in the originating Discord DM."""
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from pathlib import Path
import re
import threading

from hermes_cli.plugin_request_binding import canonical_home
from hermes_constants import get_hermes_home
from .operator_commands import operator_config
from .transport.types import Refused, digest
from .typed_plan import proposal, publish, resolve


_INBOUND = ContextVar('symphony_original_typed_command', default=None)
_DECISION = re.compile(r'/symphony (approve|deny) ([a-f0-9]{16})')
# Never evict a refusal to admit another request in the same conversation.
MAX_REQUESTS_PER_CONVERSATION = 256


def witness(event, gateway):
    """Capture the original text before alias expansion, voice transcription or command rewriting."""
    from gateway.platforms.event import MessageType
    _INBOUND.set(None)
    source, raw = event.source, event.raw_message
    if (not source or source.platform.value != 'discord' or source.chat_type != 'dm' or source.is_bot
            or source.profile_route_rejected or source.detached_route or source.delivered_via_upstream_relay
            or event.internal or not event.allow_gateway_control or event.media_urls or event.media_types
            or event.edit_revision or event.message_type not in (MessageType.TEXT, MessageType.COMMAND)
            or not isinstance(event.text, str) or not _DECISION.fullmatch(event.text) or not event.message_id):
        return
    if (getattr(raw, 'content', None) != event.text or str(getattr(raw, 'id', '')) != event.message_id
            or str(getattr(getattr(raw, 'author', None), 'id', '')) != source.user_id
            or getattr(getattr(raw, 'author', None), 'bot', None) is not False
            or str(getattr(getattr(raw, 'channel', None), 'id', '')) != source.chat_id
            or getattr(raw, 'webhook_id', True) is not None or getattr(raw, 'attachments', True)
            or getattr(raw, 'message_snapshots', True) or getattr(raw, 'edited_at', True) is not None):
        return
    key = gateway._session_key_for_source(source)
    if key and not gateway._is_session_running(key):
        _INBOUND.set((source, event.text, key))


def _owner_route(ctx):
    config = operator_config(ctx)
    route = {field: config.get(field) for field in ('platform', 'chat_id', 'user_id', 'session_key')}
    if route['platform'] != 'discord' or not all(type(v) is str and v for v in route.values()):
        raise Refused('TYPED_OWNER_ROUTE_REQUIRED')
    return route


def _submit_route(ctx, identity):
    from gateway.session_context import _VAR_MAP
    from agent.delegation_context import is_delegated_child_context
    # get_session_env falls back to the process environment; that cannot identify this caller.
    context = copy_context()
    def bound(field):
        return context.get(_VAR_MAP['HERMES_SESSION_' + field.upper()])
    route = _owner_route(ctx)
    if (is_delegated_child_context() or canonical_home(get_hermes_home()) != identity.profile_home
            or bound('id') != identity.session_id or bound('key') != route['session_key'] or bound('chat_type') != 'dm'
            or any(bound(field) != route[field] for field in ('platform', 'chat_id', 'user_id'))):
        raise Refused('TYPED_OWNER_ROUTE_REQUIRED')
    return route


def _current_session(scope, route):
    from gateway.session import SessionEntry, SessionSource
    from hermes_cli.plugin_messages import _gateway_loop
    runner, _ = _gateway_loop()
    key = route['session_key']
    if runner is None or not runner._plugin_messaging_session_owned(
            key, plugin_home=Path(scope.profile_home), plugin_id='symphony_pm'):
        raise Refused('TYPED_OWNER_ROUTE_REQUIRED')
    entry = runner.session_store.lookup_by_session_key(key)
    if (not isinstance(entry, SessionEntry) or entry.session_key != key
            or type(entry.session_id) is not str or not entry.session_id):
        raise Refused('TYPED_OWNER_ROUTE_REQUIRED')
    source = runner._restored_source(entry)
    if (not isinstance(source, SessionSource) or source.chat_type != 'dm' or source.is_bot
            or source.profile_route_rejected or source.detached_route or source.delivered_via_upstream_relay
            or source.platform.value != route['platform'] or source.chat_id != route['chat_id']
            or source.user_id != route['user_id'] or runner._session_key_for_source(source) != key):
        raise Refused('TYPED_OWNER_ROUTE_REQUIRED')
    return entry.session_id


@dataclass
class Waiting:
    request: object
    permit: object
    token: object
    route: dict
    state: str = 'PUBLISHING'


class TypedApproval:
    def __init__(self, ctx, scope, binding):
        self.ctx, self.scope = ctx, scope
        self.source = binding.authority_source
        self.owner = binding.port.owner
        if self.owner.scope is not scope or self.source.owner is not self.owner or len(binding.trusted_issuers) != 1:
            raise Refused('TYPED_OWNER_BINDING')
        self.issuer = binding.trusted_issuers[0]
        self.lock = threading.RLock()
        self.slots, self.seen, self.consumed = {}, set(), {}
        self.session_id = None
        self.owner.typed_approval = self
        self.writer = self.owner.journal.restrict_permits(self._require_submit_session)

    def _require_submit_session(self, identity):
        # Also fences a grant acquired just before the route moved, at the reserve transaction's commit.
        if _current_session(self.scope, _submit_route(self.ctx, identity)) != identity.session_id:
            raise Refused('TYPED_OWNER_ROUTE_REQUIRED')

    def _align(self, route):
        current = _current_session(self.scope, route)
        if self.session_id != current:
            self.owner.journal.retire_typed_session(self.writer)
            self.slots.clear()
            self.consumed.clear()
            self.seen.clear()
            self.session_id = current
        return current

    def expire(self):
        with self.lock:
            now, token = self.owner.clock(), self.owner.token
            for plans in (self.slots, self.consumed):
                for key, item in list(plans.items()):
                    if now >= item.permit.expires_at or token != item.token:
                        del plans[key]

    def rotated(self, session_id, reason, platform, old_session_id, new_session_id):
        # A reset hint can skip one or more compression/automatic session transitions.
        # Only the gateway's profile-owned DM index establishes the current conversation.
        if (reason != 'new_session' or platform != 'discord' or not old_session_id
                or not new_session_id or new_session_id != session_id or new_session_id == old_session_id
                or canonical_home(get_hermes_home()) != self.scope.profile_home):
            return
        with self.lock:
            self._align(_owner_route(self.ctx))

    def for_submit(self, identity, request):
        route = _submit_route(self.ctx, identity)
        key = identity.session_id
        with self.lock:
            self.expire()
            if key != self._align(route):
                return None
            if identity in self.consumed:
                return self._grant(self.consumed[identity], route)
            item = self.slots.get(key)
            if item and item.permit.identity == identity:
                if item.state != 'APPROVED':
                    return None
                return self._grant(item, route)
            fingerprint = digest(request.request_id)
            if (fingerprint in self.seen or len(self.seen) >= MAX_REQUESTS_PER_CONVERSATION
                    or (item and item.state != 'APPROVED')
                    or self.owner.journal.has_approved_request(self.owner.token, request.project_ref, request.request_id)):
                return None
            if item:
                # A new request must not displace an approved one awaiting its ordinary model retry.
                if not self.owner.journal.launch_reserved(self.owner.token, item.permit.ref):
                    return None
                self.consumed[item.permit.identity] = item
            plan, ttl = resolve(self.ctx, self.owner.launcher, request)
            token, expires = self.owner.token, self.owner.clock() + ttl
            shown, permit = proposal(identity, request, plan, route, self.issuer, token, expires)
            item = Waiting(request, permit, token, route)
            self.slots[key] = item
            self.seen.add(fingerprint)
        # Never hold the decision lock across a cross-thread gateway hand-off.
        delivered = False
        try:
            publish(self.ctx, shown, permit.contract_digest)
            delivered = _current_session(self.scope, route) == key
        finally:
            with self.lock:
                if self.slots.get(key) is item:
                    if delivered and self.owner.clock() < expires:
                        item.state = 'WAITING'
                    else:
                        self.slots.pop(key)
        return None

    def _grant(self, item, route):
        if route != item.route or resolve(self.ctx, self.owner.launcher, item.request)[0] != item.permit.plan:
            return None
        grant = self.source(item.permit.identity)
        if grant and grant.grant_ref == item.permit.ref and grant.contract_digest == item.permit.contract_digest:
            return grant
        return None

    def decide(self, raw_args, source, session_key, session_id):
        from gateway.plugin_messages import bound_route
        proof = _INBOUND.get()
        if (not proof or proof[0] is not source or proof[1:] != ('/symphony ' + raw_args, session_key)
                or bound_route() is not source or not session_id):
            raise Refused('ORIGINAL_TYPED_COMMAND_REQUIRED')
        action, code = _DECISION.fullmatch(proof[1]).groups()
        with self.lock:
            self.expire()
            route = _owner_route(self.ctx)
            if self._align(route) != session_id:
                raise Refused('NO_MATCHING_TYPED_PLAN')
            item = self.slots.get(session_id)
            if (not item or item.state != 'WAITING' or item.permit.contract_digest[:16] != code
                    or item.route != route):
                raise Refused('NO_MATCHING_TYPED_PLAN')
            if action == 'deny':
                self.slots.pop(session_id)
                return 'DENIED'
            # Retire first. Failure or uncertainty while recording consumes this decision.
            self.slots.pop(session_id)
            if resolve(self.ctx, self.owner.launcher, item.request)[0] != item.permit.plan:
                raise Refused('TYPED_PLAN_CHANGED')
            with self.owner.lock:
                def authorize_commit():
                    if _current_session(self.scope, item.route) != session_id:
                        raise Refused('TYPED_OWNER_ROUTE_REQUIRED')
                    self.owner._check(item.token)
                    item.permit.validate(self.owner.clock())
                authorize_commit()
                self.owner.journal.record_permit(item.token, item.permit, 'discord-' + code,
                    item.permit.contract_digest, self.owner.clock(), writer=self.writer, authorize_commit=authorize_commit)
            item.state = 'APPROVED'
            self.slots[session_id] = item
            return 'APPROVED. Ask the agent to continue the original request with the same request_id.'
