"""Trusted incoming commands for the owner of one Symphony profile."""
from dataclasses import asdict
import json
from uuid import uuid4

from hermes_cli.plugin_request_binding import canonical_home
from hermes_constants import get_hermes_home

from .status_cli import read_launches, summary
from .transport.types import Refused


def operator_config(ctx):
    get_config = getattr(ctx, 'get_config', None)
    config = get_config('operator', {}) if get_config else {}
    return config if isinstance(config, dict) else {}


def _owner_from(ingress):
    if ingress is None:
        return None
    owner = getattr(getattr(ingress.binding, 'port', None), 'owner', None)
    return owner if getattr(owner, 'scope', None) is ingress.scope else None


def attach_operator(ctx, ingress):
    from .doctor import record_qualification
    # ServiceReference is published after the factory returns. The host has already
    # bound the profile and will run on_close if initializing any service resource fails.
    owner = _owner_from(ingress)
    if owner is not None:
        from .retention import linux_retention_unavailable, start_weekly
        record_qualification(owner)
        if linux_retention_unavailable(owner):
            return ingress  # The Linux archive pair has no mount-safe deletion contract.
        config = operator_config(ctx)
        if all(isinstance(config.get(k), str) and config[k].strip() for k in ('platform', 'chat_id', 'user_id', 'session_key')):
            start_weekly(ctx, owner, config)
    return ingress


def _authorized(config, source, session_key, platform):
    from gateway.session import SessionSource
    return (isinstance(source, SessionSource) and not source.is_bot and not source.profile_route_rejected
            and bool(session_key) and bool(source.user_id) and bool(source.chat_id)
            and source.platform.value == platform == config.get('platform')
            and source.user_id == config.get('user_id') and source.chat_id == config.get('chat_id')
            and session_key == config.get('session_key'))


def viewer_job_controls_held(scope):
    if scope.namespace != 'symphony_pm:ingress-linux':
        return False
    from gateway.session_ingress import adapter_admission, executing_admission
    from gateway.session_viewer_command import is_viewer_command
    return is_viewer_command() or (executing_admission.get() and not adapter_admission.get())


def _dispatch(owner, config, args):
    words = args.strip().split()
    if not words:
        raise Refused('usage: /symphony status|stop|release|recapture|prune')
    action, *args = words
    if action == 'prune':
        from .retention import prune
        if not args:
            return prune(owner, config)
        if len(args) == 2 and args[0] == 'approve':
            return prune(owner, config, args[1])
        raise Refused('usage: /symphony prune [approve CODE]')
    rows = [r for r in read_launches(owner.journal.path) or [] if r['namespace'] == owner.scope.namespace]
    if action == 'status' and not args:
        return {'jobs': [{'project': r['project'], 'job': r['job'], 'revision': r['revision'],
                          'state': r['state'], 'view': summary(r)['view']} for r in rows]}
    if action not in ('stop', 'status', 'release', 'recapture') or not args:
        raise Refused('JOB_COMMAND_REQUIRED')
    job, *options = args
    matches = [r for r in rows if r['job'] == job]
    if len(matches) != 1 or not matches[0]['revision']:
        raise Refused('JOB_IDENTITY')
    row = matches[0]
    target = (row['project'], row['job'], row['revision'])
    if action == 'status' and options == ['--saved']:
        result = {'view': summary(row)['view']}
        apply = next((obs['body'] for obs in reversed(row.get('observations', [])) if obs['kind'] == 'APPLY'), None)
        if apply is not None:
            result['apply'] = apply
        return result
    if options and action != 'release':
        raise Refused('UNEXPECTED_ARGUMENT')
    if action == 'status':
        return {'view': owner.observe_status(*target)}
    if action == 'stop':
        return asdict(owner.stop_by_owner(*target, request_id='human-' + uuid4().hex))
    if action == 'release':
        owner.release_by_owner(owner.token, *target, ' '.join(options))
        return {'state': 'RELEASED_BY_OWNER'}
    captured = owner.recapture(owner.token, *target)
    return {'seq': captured['seq'], 'patch_sha256': captured['patch']['sha256']}


def register_operator(ctx, *references):
    def active_ingress():
        home = canonical_home(get_hermes_home())
        services = [service for ref in references if ref is not None and (service := ref.get()) is not None
                    if (owner := _owner_from(service)) is not None and owner.scope.active
                    and owner.scope.profile_home == home]
        return services[0] if len(services) == 1 else None

    if getattr(ctx, 'register_hook', None):
        from asyncio import to_thread
        from .typed_approval import witness
        def active_approval():
            service = active_ingress()
            return getattr(service, 'typed_approval', None)

        def observe_typed_input(event, gateway):
            # Maintenance commands also need the original text in profiles without plan approval.
            witness(event, gateway)
            approval = active_approval()
            if approval is not None:
                return to_thread(approval.expire)

        def session_reset(session_id='', reason='', platform='', old_session_id='', new_session_id=''):
            approval = active_approval()
            if approval is not None:
                approval.rotated(session_id, reason, platform, old_session_id, new_session_id)
        ctx.register_hook('pre_gateway_dispatch', observe_typed_input)
        ctx.register_hook('on_session_reset', session_reset)

    def command(raw_args, *, source=None, session_key='', session_id='', platform=''):
        config = operator_config(ctx)
        if not _authorized(config, source, session_key, platform):
            return 'OWNER_SENDER_REQUIRED'
        ingress = active_ingress()
        owner = _owner_from(ingress)
        if owner is None or not owner.scope.active or owner.scope.profile_home != canonical_home(get_hermes_home()):
            return 'OWNER_UNAVAILABLE'
        try:
            verb = raw_args.split()[:1]
            if verb in (['plan-presented'], ['plan-answer'], ['plan-receipt'], ['plan-result']):
                from .desktop_approval import viewer_action
                return json.dumps(viewer_action(getattr(ingress, 'typed_approval', None), source, session_id, raw_args),
                                  ensure_ascii=False)
            if verb in (['approve'], ['deny'], ['apply']):
                approval = getattr(ingress, 'typed_approval', None)
                if approval is None:
                    raise Refused('TYPED_APPROVAL_UNAVAILABLE')
                if verb == ['apply']:
                    return json.dumps(approval.apply(raw_args, source, session_key, session_id), sort_keys=True)
                return approval.decide(raw_args, source, session_key, session_id)
            if platform == 'local' and verb in (['release'], ['prune']):
                raise Refused('ORIGINAL_TYPED_COMMAND_REQUIRED')
            # The local gateway gives the handler the conversation source. It does not
            # authenticate the remote SSH participant as the operator of a job.
            controls_held = viewer_job_controls_held(owner.scope)
            if controls_held and verb in (['stop'], ['release'], ['prune'], ['recapture']):
                raise Refused('TASK_OPERATOR_REQUIRED')
            if controls_held and verb == ['status'] and raw_args.strip() != 'status':
                raise Refused('TASK_OPERATOR_REQUIRED')
            approval = getattr(ingress, 'typed_approval', None)
            if verb in (['release'], ['prune']):
                if approval is None:
                    from .typed_approval import original_text
                    original_text(raw_args, source, session_key)
                else:
                    approval.require_typed_maintenance(raw_args, source, session_key, session_id)
            result = ({'jobs': []} if controls_held and verb == ['status']
                      else _dispatch(owner, config, raw_args))
            if platform == 'local' and raw_args.strip() == 'status' and approval is not None:
                from .desktop_approval import pending_plan
                from gateway.session_viewer_command import original_viewer_command
                viewer = original_viewer_command(source, 'symphony', raw_args)
                pending = pending_plan(approval, session_id) if viewer else None
                if pending is not None:
                    result['pending_plan'] = pending
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except Refused as exc:
            return str(exc)
    return ctx.register_command('symphony', command, description='Manage Symphony attempts in this profile',
                                args_hint='approve|deny|apply|status|stop|release|recapture|prune')
