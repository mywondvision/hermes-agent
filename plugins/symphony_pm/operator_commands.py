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
        return {'view': summary(row)['view']}
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
    def command(raw_args, *, source=None, session_key='', platform=''):
        config = operator_config(ctx)
        if not _authorized(config, source, session_key, platform):
            return 'OWNER_SENDER_REQUIRED'
        home = canonical_home(get_hermes_home())
        owners = [owner for ref in references if ref is not None
                  if (owner := _owner_from(ref.get())) is not None and owner.scope.active
                  and owner.scope.profile_home == home]
        if len(owners) != 1:
            return 'OWNER_UNAVAILABLE'
        try:
            return json.dumps(_dispatch(owners[0], config, raw_args), ensure_ascii=False, sort_keys=True)
        except Refused as exc:
            return str(exc)
    return ctx.register_command('symphony', command, description='Manage Symphony attempts in this profile',
                                args_hint='status|stop|release|recapture|prune')
