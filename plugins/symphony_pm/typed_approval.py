"""One visible plan and a typed decision in the originating Discord DM."""
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from pathlib import Path
import re
import threading

from hermes_cli.plugin_request_binding import canonical_home
from hermes_constants import get_hermes_home
from .operator_commands import operator_config
from .transport.types import Refused, digest
from .typed_plan import proposal, publish, resolve


_INBOUND = ContextVar('symphony_original_typed_command', default=None)
_VOICE_EVENT = ContextVar('symphony_gateway_voice_event', default=None)
_DECISION = re.compile(r'/symphony (approve|deny) ([a-f0-9]{16})')
_APPLY = re.compile(r'/symphony apply ([A-Za-z0-9][A-Za-z0-9_.:-]{0,127}) ([0-9a-f]{1,64})'
                    r'(?: (--accept-failed-git-checks|--show-failed-git-checks))?')
_MAINTENANCE = re.compile(r'/symphony (?:release|prune)(?: [^\r\n]+)?')
# Never evict a refusal to admit another request in the same conversation.
MAX_REQUESTS_PER_CONVERSATION = 256


def _telegram_maintenance(event, source, raw):
    sender = getattr(raw, 'from_user', None)
    chat = getattr(raw, 'chat', None)
    return (_MAINTENANCE.fullmatch(event.text) is not None
            and getattr(raw, 'text', None) == event.text and str(getattr(raw, 'message_id', '')) == event.message_id
            and str(getattr(sender, 'id', '')) == source.user_id and getattr(sender, 'is_bot', None) is False
            and str(getattr(chat, 'id', '')) == source.chat_id and getattr(chat, 'type', None) == 'private'
            and all(getattr(raw, field, True) is None for field in (
                'edit_date', 'forward_origin', 'via_bot', 'sender_chat', 'voice', 'audio',
                'business_connection_id', 'sender_business_bot')))


def original_text(raw_args, source, session_key):
    from gateway.plugin_messages import bound_route
    proof = _INBOUND.get()
    if (not proof or proof[0] is not source or proof[1:3] != ('/symphony ' + raw_args, session_key)
            or bound_route() is not source):
        raise Refused('ORIGINAL_TYPED_COMMAND_REQUIRED')
    return proof[1]


def witness(event, gateway):
    """Capture the original text before alias expansion, voice transcription or command rewriting."""
    from gateway.platforms.event import MessageType
    _INBOUND.set(None)
    source, raw = event.source, event.raw_message
    _VOICE_EVENT.set(None)
    if source and event.message_type == MessageType.VOICE:
        _VOICE_EVENT.set((gateway._session_key_for_source(source), event.message_id))
    if (not source or source.platform.value not in ('discord', 'telegram') or source.chat_type != 'dm' or source.is_bot
            or source.profile_route_rejected or source.detached_route or source.delivered_via_upstream_relay
            or event.internal or not event.allow_gateway_control or event.media_urls or event.media_types
            or event.edit_revision or event.message_type not in (MessageType.TEXT, MessageType.COMMAND)
            or not isinstance(event.text, str)
            or not any(rule.fullmatch(event.text) for rule in (_DECISION, _APPLY, _MAINTENANCE))
            or not event.message_id):
        return
    if source.platform.value == 'telegram':
        if not _telegram_maintenance(event, source, raw):
            return
    elif (getattr(raw, 'content', None) != event.text or str(getattr(raw, 'id', '')) != event.message_id
            or str(getattr(getattr(raw, 'author', None), 'id', '')) != source.user_id
            or getattr(getattr(raw, 'author', None), 'bot', None) is not False
            or str(getattr(getattr(raw, 'channel', None), 'id', '')) != source.chat_id
            or getattr(raw, 'webhook_id', True) is not None or getattr(raw, 'attachments', True)
            or getattr(raw, 'message_snapshots', True) or getattr(raw, 'edited_at', True) is not None):
        return
    key = gateway._session_key_for_source(source)
    if key and not gateway._is_session_running(key):
        _INBOUND.set((source, event.text, key, str(event.message_id)))


def _owner_route(ctx):
    config = operator_config(ctx)
    route = {field: config.get(field) for field in ('platform', 'chat_id', 'user_id', 'session_key')}
    if route['platform'] not in ('discord', 'local') or not all(type(v) is str and v for v in route.values()):
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
    if route['platform'] == 'local':
        from .desktop_approval import current_session
        return current_session(runner, scope, route)
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
    shown: dict | None = None
    admission_id: str | None = None
    presented_to: set = field(default_factory=set)


class TypedApproval:
    def __init__(self, ctx, scope, binding):
        self.ctx, self.scope = ctx, scope
        self.binding = binding
        self.source = binding.authority_source
        self.owner = binding.port.owner
        if self.owner.scope is not scope or self.source.owner is not self.owner or len(binding.trusted_issuers) != 1:
            raise Refused('TYPED_OWNER_BINDING')
        self.issuer = binding.trusted_issuers[0]
        self.lock = threading.RLock()
        self.apply_lock = threading.Lock()
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
            voice = _VOICE_EVENT.get()
            if route['platform'] == 'local':
                from gateway.session_ingress import viewer_admission_id, viewer_turn
                live = viewer_turn.get()
                if live is None or live.source.chat_id != route['chat_id']:
                    return None
                voice = (live.route, viewer_admission_id.get())
            if voice is not None and (voice[0] != route['session_key'] or not voice[1]):
                return None
            if not self.owner.journal.admit_plan_input(token, permit, route['session_key'],
                                                       voice[1] if voice else None, writer=self.writer):
                return None
            item = Waiting(request, permit, token, route)
            self.slots[key] = item
            self.seen.add(fingerprint)
            if route['platform'] == 'local':
                item.shown = shown
                item.admission_id = voice[1]
                item.state = 'AWAITING_REVIEW'
                from .desktop_approval import notify_plan
                notify_plan(live, item)
                return None
        # Never hold the decision lock across a cross-thread gateway hand-off.
        delivered = False
        try:
            message_id = publish(self.ctx, shown, permit.contract_digest)
            delivered = _current_session(self.scope, route) == key
        finally:
            with self.lock:
                if self.slots.get(key) is item:
                    if delivered and self.owner.clock() < expires:
                        self.owner.journal.record_plan_event(token, permit, 'WAITING', message_id,
                                                             self.owner.clock(), writer=self.writer)
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

    def _original_command(self, raw_args, source, session_key, session_id):
        if not session_id:
            raise Refused('ORIGINAL_TYPED_COMMAND_REQUIRED')
        return original_text(raw_args, source, session_key)

    def apply(self, raw_args, source, session_key, session_id):
        from .transport.robot_apply import apply_checked
        text = self._original_command(raw_args, source, session_key, session_id)
        match = _APPLY.fullmatch(text)
        if match is None:
            raise Refused('ROBOT_APPLY_COMMAND_REQUIRED')
        job, prefix, option = match.groups()
        with self.apply_lock:
            route = _owner_route(self.ctx)
            def authorize():
                self._original_command(raw_args, source, session_key, session_id)
                if _owner_route(self.ctx) != route or _current_session(self.scope, route) != session_id:
                    raise Refused('TYPED_OWNER_ROUTE_REQUIRED')
                if self.ctx.get_config('typed_approval', {}) != config:
                    raise Refused('ROBOT_PROJECT_BINDING')
            config = self.ctx.get_config('typed_approval', {})
            if not isinstance(config, dict) or not config.get('project_ref'):
                raise Refused('ROBOT_PROJECT_BINDING')
            def present_failure(record):
                from .typed_plan import deliver_part
                failures = [f"{item['name']} (exit {item['exit_code']})" for item in record['git_checks'] if item['exit_code']]
                full = record['identity']['patch_sha256']
                parts = [f"Git check failed for {job}, patch {full}: {failure}" for failure in failures]
                parts.append(f"To accept these failures: /symphony apply {job} {full} --accept-failed-git-checks")
                for part in parts:
                    authorize()
                    message_id = deliver_part(self.ctx, route['chat_id'], part)
                return dict(message_id=str(message_id), session_id=session_id, chat_id=route['chat_id'])
            witness_id = _INBOUND.get()[3]
            return apply_checked(self.owner, config, job, prefix, option, authorize, present_failure,
                                 dict(message_id=witness_id, session_id=session_id, chat_id=route['chat_id']))

    def require_typed_maintenance(self, raw_args, source, session_key, session_id):
        self._original_command(raw_args, source, session_key, session_id)
        if _current_session(self.scope, _owner_route(self.ctx)) != session_id:
            raise Refused('TYPED_OWNER_ROUTE_REQUIRED')

    def decide(self, raw_args, source, session_key, session_id):
        text = self._original_command(raw_args, source, session_key, session_id)
        action, code = _DECISION.fullmatch(text).groups()
        with self.lock:
            self.expire()
            route = _owner_route(self.ctx)
            if self._align(route) != session_id:
                raise Refused('NO_MATCHING_TYPED_PLAN')
            item = self.slots.get(session_id)
            if (not item or item.permit.contract_digest[:16] != code
                    or item.route != route):
                raise Refused('NO_MATCHING_TYPED_PLAN')
            if item.state == 'DENIAL_UNKNOWN':
                if action != 'deny' or not self.owner.journal.recorded_denial(item.token, item.permit, writer=self.writer):
                    raise Refused('DENIAL_OUTCOME_UNKNOWN')
                self.slots.pop(session_id)
                return 'DENIED'
            if item.state != 'WAITING':
                raise Refused('NO_MATCHING_TYPED_PLAN')
            if action == 'deny':
                item.state = 'DENIAL_UNKNOWN'
                try:
                    self.owner.journal.record_plan_event(item.token, item.permit, 'DENIED', _INBOUND.get()[3],
                                                         self.owner.clock(), writer=self.writer)
                except Refused:
                    if not self.owner.journal.recorded_denial(item.token, item.permit, writer=self.writer):
                        raise Refused('DENIAL_OUTCOME_UNKNOWN') from None
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
