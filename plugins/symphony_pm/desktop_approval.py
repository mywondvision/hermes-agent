"""Presentation and single-use answers for the configured Desktop conversation."""
import json
import re
import subprocess
from hermes_cli.plugin_request_binding import canonical_home
from .transport.types import Refused, digest

PLAN_EVENT = "symphony.plan"


def current_session(runner, scope, route, *, routing_lookup=None):
    from gateway.session_authorities import all_authorities
    from gateway.session_policy import policy_for_source
    if runner is not None:
        for authority in all_authorities(runner):
            if canonical_home(authority.profile_id) != scope.profile_home:
                continue
            live = authority.sessions.get(route['chat_id'])
            if live is None or live.route != route['session_key']:
                continue
            if (live.source.platform.value != 'local' or live.source.user_id != route['user_id']
                    or live.source.chat_id != route['chat_id']):
                continue
            policy = policy_for_source(runner, live.source)
            if policy is not None and policy.source == 'gui':
                if routing_lookup is not None:
                    session_id = routing_lookup(live.route)
                    if session_id:
                        return session_id
                    continue
                entry = runner.session_store.lookup_by_session_key(live.route)
                if entry and entry.session_key == live.route and entry.session_id:
                    return entry.session_id
    raise Refused('TYPED_OWNER_ROUTE_REQUIRED')


def pending_plan(approval, session_id):
    from .typed_approval import _owner_route
    with approval.lock:
        approval.expire()
        route = _owner_route(approval.ctx)
        if approval._align(route) != session_id:
            return None
        item = approval.slots.get(session_id)
        if item and item.state == 'AWAITING_REVIEW' and item.route == route:
            _verify_pending(approval, session_id, item)
            return dict(state='WAITING_FOR_AUTHORITY', request_id=item.request.request_id,
                        admission_id=item.admission_id, digest=item.permit.contract_digest, plan=item.shown)
        return None


def _verify_pending(approval, session_id, item):
    # The caller holds approval.lock. A failed read cannot leave the old display actionable.
    approval.slots.pop(session_id)
    _verify_plan(approval, item)
    approval.slots[session_id] = item


def _verify_plan(approval, item):
    from .typed_plan import resolve
    try:
        current, _ = resolve(approval.ctx, approval.owner.launcher, item.request)
    except (Refused, OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError) as exc:
        raise Refused('TYPED_PLAN_CHANGED') from exc
    if current != item.permit.plan or digest(item.shown) != item.permit.contract_digest:
        raise Refused('TYPED_PLAN_CHANGED')


def notify_plan(live, item):
    from hermes_cli.plugin_messages import _gateway_loop
    from gateway.session_authorities import all_authorities
    from gateway.session_server_requests import may_settle
    runner, loop = _gateway_loop()
    if loop is None:
        return
    chat = live.source.chat_id
    def deliver():
        for authority in all_authorities(runner):
            if (canonical_home(authority.profile_id) != item.permit.identity.profile_home
                    or authority.sessions.get(chat) is not live):
                continue
            stream = live.event_stream
            if hasattr(stream, 'private_subscribers'):
                with stream.lock:
                    receivers = tuple(stream.private_subscribers.get(PLAN_EVENT, ()))
            else:
                receivers = ()
            for receiver in receivers:
                receiver()
            for actor in tuple(getattr(live, 'subscribers', {}).values()):
                transport = authority.events.get(actor.transport_id)
                identity = getattr(transport, 'auth_identity', None) or {}
                if identity.get('native_bootstrap') and may_settle(authority, actor, chat, stream):
                    transport.write(dict(jsonrpc='2.0', method='event', params=dict(type=PLAN_EVENT, session_id=chat,
                                         payload=dict(plan_digest=item.permit.contract_digest))))
    loop.call_soon_threadsafe(deliver)


_ANSWERS = {'подтверждаю': 'APPROVED', 'отклоняю': 'DENIED'}


def decide_presented(approval, session_id, reply, *, viewer, operation, check_viewer=None, commit_guard=None):
    """The caller supplies the verified native connection, never a viewer id from the wire."""
    from .typed_approval import _current_session, _owner_route
    route = _owner_route(approval.ctx)
    owner, journal = approval.owner, approval.owner.journal
    if (route['platform'] != 'local' or not viewer or not isinstance(reply, dict)
            or _current_session(approval.scope, route) != session_id):
        raise Refused('DESKTOP_DECISION_ROUTE')
    with approval.lock:
        if check_viewer is not None:
            check_viewer()
        approval.expire()
        if approval._align(route) != session_id:
            raise Refused('DESKTOP_DECISION_ROUTE')
        key = reply.get('input_id')
        if operation == 'result':
            previous = journal.desktop_receipt(owner.token, session_id, reply.get('plan_digest'),
                                               writer=approval.writer, by_plan=True)
            if not previous or previous['request_id'] != reply.get('request_id'):
                raise Refused('DESKTOP_DECISION_UNKNOWN')
            return previous
        if operation != 'presented':
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', key):
                raise Refused('DESKTOP_INPUT_ID_REQUIRED')
            previous = journal.desktop_receipt(owner.token, session_id, key, writer=approval.writer)
            if previous is not None:
                if ((previous['plan_digest'], previous['request_id']) !=
                        (reply.get('plan_digest'), reply.get('request_id'))):
                    raise Refused('DESKTOP_INPUT_CONFLICT')
                if operation == 'answer' and (previous['decision'] != _decision(reply)
                        or previous['input_kind'] != reply.get('input_kind')):
                    raise Refused('DESKTOP_INPUT_CONFLICT')
                return previous
            if operation == 'receipt':
                raise Refused('DESKTOP_DECISION_UNKNOWN')
        item = approval.slots.get(session_id)
        if (not item or item.state != 'AWAITING_REVIEW' or route != item.route
                or item.permit.contract_digest != reply.get('plan_digest')
                or item.request.request_id != reply.get('request_id')):
            raise Refused('NO_MATCHING_DESKTOP_PLAN')
        _verify_pending(approval, session_id, item)
        if operation == 'presented':
            item.presented_to.add(viewer)
            return {'state': 'PRESENTED', 'plan_digest': item.permit.contract_digest}
        if operation != 'answer' or viewer not in item.presented_to:
            raise Refused('DESKTOP_PLAN_NOT_PRESENTED')
        action = _decision(reply)
        if reply.get('input_kind') not in ('text', 'voice') or not action:
            raise Refused('EXPLICIT_PLAN_DECISION_REQUIRED')
        def authorize():
            if check_viewer is not None:
                check_viewer()
            if _current_session(approval.scope, route) != session_id or _owner_route(approval.ctx) != route:
                raise Refused('DESKTOP_DECISION_ROUTE')
            owner._check(item.token)
            item.permit.validate(owner.clock())
        def authorize_commit():
            _verify_plan(approval, item)
            authorize()
            if commit_guard is not None:
                return commit_guard()
        receipt = dict(profile=approval.scope.profile_home, namespace=approval.scope.namespace, session_id=session_id,
                       request_id=item.request.request_id, plan_digest=item.permit.contract_digest, input_id=key,
                       input_kind=reply['input_kind'], decision=action, shown_plan=item.shown)
        with owner.lock:
            authorize()
            # A failed commit may already be durable. Only receipt lookup may resolve it.
            approval.slots.pop(session_id)
            journal.record_desktop_decision(item.token, item.permit, receipt, writer=approval.writer,
                                            authorize_commit=authorize_commit)
        if action == 'DENIED':
            return receipt
        grant = approval._grant(item, route)
    # If handoff is interrupted, the durable decision remains readable. Only the first caller
    # may hand the original request to the regular reservation path.
    if grant is None:
        return dict(receipt, launch='UNKNOWN')
    result = approval.binding.port.submit_intent(item.request, grant.value)
    return dict(receipt, launch=result.outcome, job=result.job_ref)


def _decision(reply):
    text = reply.get('text')
    return _ANSWERS.get(text.strip().casefold()) if isinstance(text, str) else None


def viewer_action(approval, source, session_id, args):
    from gateway.session_viewer_command import original_viewer_command
    viewer = original_viewer_command(source, 'symphony', args)
    if not viewer or approval is None:
        raise Refused('DESKTOP_DECISION_ROUTE')
    verb, _, body = args.partition(' ')
    try:
        reply = json.loads(body)
    except ValueError:
        raise Refused('DESKTOP_DECISION_BINDING') from None
    return decide_presented(approval, session_id, reply, viewer=viewer, operation=verb.removeprefix('plan-'))
