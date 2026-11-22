"""The owner's durable, attempt-bound status and its projection into the PM receipt contract."""
from dataclasses import asdict
import json
from uuid import uuid4

from plugins.symphony_pm import adapter as pm
from .types import Refused, finite, sha


def phase(owner, row):
    state = row['state']
    if state == 'LAUNCH_INTENT':
        return 'PENDING'
    if state in ('NOT_STARTED', 'HELD_UNKNOWN', 'RELEASED_BY_OWNER'):
        return state
    if state == 'STOPPED':
        if not owner.journal.proven_stop(row):
            return 'HELD_UNKNOWN'
        result = json.loads(row['result']) if row['result'] else {}
        if (isinstance(result, dict) and type(result.get('exit_code')) is int and result['exit_code'] == 0
                and 'stop' in result and result['stop'] is None):
            return 'COMPLETED'
        return 'STOPPED'
    if not row['binding']:
        return 'HELD_UNKNOWN'
    try:
        binding = owner.journal.decode_binding(json.loads(row['binding']))
        owner._refresh_observer()
        accounting = owner._job_accounting(binding)
        return accounting if owner._coverage_verified() else 'HELD_UNKNOWN'
    except Exception:  # health: allow BLE001 -- an unavailable or malformed observer cannot attest a running Job
        return 'HELD_UNKNOWN'


def sandbox_checks(owner, ticket, row):
    """Only the last capture and its bound CHECK can attest the owner-configured sandbox stage."""
    capture, check = owner.journal.stage_records(owner.token, ticket)
    return checked_commands(owner, ticket, row, capture, check)


def checked_commands(owner, ticket, row, capture, check):
    patch = capture.get('patch') or {}
    if (capture.get('source_sha') != ticket.plan.source_sha or not isinstance(patch, dict)
            or not sha(patch.get('sha256'), 64) or not isinstance(patch.get('paths'), list)
            or not patch['paths'] or not all(type(path) is str and path for path in patch['paths'])):
        return (), ()
    try:
        plan, policy_matches = owner.check_policy(ticket, row, patch, check)
    except Exception:  # health: allow BLE001 -- status remains available when the owner cannot establish the plan
        return (), ()
    required = tuple(c.name for c in plan if not c.requires_git)
    deferred = [c.name for c in plan if c.requires_git]
    commands = check.get('commands')
    if (not policy_matches or check.get('execution_hold') is not None
            or not required or type(commands) is not dict or set(commands) != set(required)
            or check.get('deferred') != deferred or type(check.get('apply')) is not int or check['apply'] != 0
            or not ((not deferred and check.get('ok') is True and not check.get('reason'))
                or (deferred and check.get('ok') is False and check.get('reason') == 'CHECK_GIT_TESTS_DEFERRED'))):
        return required, ()
    return required, tuple((name, 'pass' if type(code) is int and code == 0 else 'fail')
                           for name, code in commands.items())


def observe(owner, ticket, row, *, request=None):
    row = owner.status_row(ticket, row)
    state = phase(owner, row)
    required, checks = (), ()
    if state in pm.DONE_PHASES and owner.journal.proven_stop(row):
        required, checks = sandbox_checks(owner, ticket, row)
    seq = owner.journal.next_observation_seq(owner.token, ticket)
    ref = f'status:{seq}:{uuid4().hex}'
    now = owner.clock()
    if not finite(now):
        raise Refused('STATUS_TIME')
    producer = owner.token.incarnation
    obs = pm.TrustedObservation(producer, ref, ticket.job, ticket.revision, ticket.attempt, owner.token.epoch,
                                state, now)
    uncertainty = ['EXTERNAL_AND_BILLING_UNKNOWN']
    if state == 'HELD_UNKNOWN':
        uncertainty.append('LOCAL_STATE_UNPROVEN')
    acceptance, _ = pm._stage_acceptance(state, required, (checks,))
    view = pm._status_view(obs, now, acceptance, [['reservation', 'POSSIBLY_CONSUMED_HELD']], uncertainty)
    body = {'kind': 'STATUS', 'view': view, 'observation': asdict(obs), 'required_checks': required,
            'checks': checks, 'containment_empty': owner.journal.containment_empty(row),
            'request_digest': request.request_digest if request else None}
    token = owner.token
    owner.journal.save_status(token, ticket, body, seq, authorize_commit=lambda: owner._check(token))
    return body


def evidence(body):
    obs = pm.TrustedObservation(**body['observation'])
    target = (obs.producer, obs.evidence_ref, obs.job_ref, obs.job_revision)
    completions = (pm.TrustedCompletion(*target, obs.attempt_ref, True, tuple(map(tuple, body['checks']))),
                   ) if obs.phase in pm.DONE_PHASES else ()
    claims = tuple(pm.TrustedClaim(*target, claim) for claim in ('worker_stopped', 'descendants_stopped')
                   if body.get('containment_empty', body.get('job_empty')) is True)
    return dict(trusted_producers=(obs.producer,), trusted_observations=(obs,), trusted_completions=completions,
                required_checks=tuple(body['required_checks']), trusted_claims=claims)
