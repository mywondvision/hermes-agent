"""Resolve the current profile's one project and the installed launcher's exact configuration."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

from hermes_platform.resolver import locate_command
from .adapter import canonical_bytes
from .transport.types import Permit, Refused, TypedExecutionPlan, digest, finite, sha


def branch_commit(repository, branch):
    git = locate_command('git').command
    if not git or not isinstance(branch, str) or not branch or branch.startswith('-'):
        raise Refused('SOURCE_BRANCH_REQUIRED')
    environment = {k: os.environ[k] for k in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP') if k in os.environ}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS='1',
                       GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1')
    def read(*args):
        completed = subprocess.run([git[0], '-C', repository, *args], env=environment, timeout=5,
                                   stdin=subprocess.DEVNULL, capture_output=True, check=False)
        if completed.returncode:
            raise Refused('SOURCE_BRANCH_UNAVAILABLE')
        return completed.stdout.decode('ascii').strip()
    ref = 'refs/heads/' + branch
    read('check-ref-format', ref)
    commit = read('rev-parse', '--verify', '--end-of-options', ref + '^{commit}')
    if not sha(commit, 40):
        raise Refused('SOURCE_COMMIT_REQUIRED')
    return commit


def resolve(ctx, launcher, request):
    config = ctx.get_config('typed_approval', None)
    if not isinstance(config, dict) or config.get('project_ref') != request.project_ref:
        raise Refused('TYPED_APPROVAL_PROJECT')
    repository = Path(config['repository'])
    if not repository.is_absolute() or not repository.is_dir():
        raise Refused('TYPED_APPROVAL_REPOSITORY')
    executor = launcher.typed_plan()
    if (Path(executor['repository']).resolve(strict=True) != repository.resolve(strict=True)
            or not Path(executor['workspace']).is_absolute()
            or not sha(executor['executor_sha256'], 64)
            or not isinstance(executor['checks'], list) or not executor['checks']
            or not finite(executor['exec_timeout']) or executor['exec_timeout'] <= 0):
        raise Refused('TYPED_APPROVAL_EXECUTOR')
    ttl = config.get('ttl_seconds', 900)
    if type(ttl) is not int or not 1 <= ttl <= 3600:
        raise Refused('TYPED_APPROVAL_TTL')
    branch = config['branch']
    details = dict(executor, repository=str(repository.resolve()), branch=branch, ttl_seconds=ttl,
                   source_sha=branch_commit(str(repository), branch), **request.payload(), request_id=request.request_id)
    plan = TypedExecutionPlan(executor['model'], executor['effort'], executor['environment_ref'],
                              executor['workspace'], details['source_sha'],
                              typed_details=canonical_bytes(details).decode('utf-8'))
    plan.validate()
    return plan, ttl


def proposal(identity, request, plan, route, issuer, token, expires_at):
    ref = 'typed-' + uuid4().hex
    details = json.loads(plan.typed_details)
    shown = dict(details, identity=asdict(identity), route=route, issuer_ref=issuer,
                 expires_at_utc=datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
                 execution_time_limit_seconds=details['exec_timeout'],
                 writer_epoch=token.epoch, permit_ref=ref, expires_at=expires_at, attempt_budget=1, driver=plan.driver)
    fingerprint = digest(shown)
    permit = Permit(ref, identity, request.project_ref, request.request_digest, fingerprint, 'typed-owner',
                    'typed-plan-v1', issuer, expires_at, plan)
    return shown, permit


def publish(ctx, shown, fingerprint):
    from gateway.plugin_messages import _fit
    # Discord DMs can carry 2000 UTF-16 units. Keep chunks well under that bound even for astral
    # characters. Escape chat formatting characters inside the JSON so model-provided text is data.
    body = json.dumps(shown, ensure_ascii=False, indent=2, sort_keys=True)
    for char in ('\x60', '@', '<', '>', '\u202a', '\u202b', '\u202d', '\u202e', '\u2066', '\u2067', '\u2068', '\u2069'):
        body = body.replace(char, '\\u' + format(ord(char), '04x'))
    code = fingerprint[:16]
    controls = f'SHA-256 {fingerprint}\n/symphony approve {code}\n/symphony deny {code}'
    messages = ['\x60\x60\x60json\n' + body[i:i + 880] + '\n\x60\x60\x60' for i in range(0, len(body), 880)]
    if len(messages[-1]) + len(controls) < 900:
        messages[-1] += '\n' + controls
    else:
        messages.append(controls)
    for part in messages:
        handle = ctx.post_message(part)
        if not handle.ok or not handle.message_id or handle.result.get('delivered') is False:
            raise Refused('TYPED_PLAN_DELIVERY_FAILED')
        target = getattr(handle, '_target', None)
        if (target is None or target.platform != 'discord' or target.chat_id != shown['route']['chat_id']
                or _fit(target.adapter, target.chat_id, part) != part):
            raise Refused('TYPED_PLAN_DELIVERY_INCOMPLETE')
