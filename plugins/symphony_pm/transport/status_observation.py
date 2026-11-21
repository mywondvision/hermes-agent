"""The owner's durable, attempt-bound status and its projection into the PM receipt contract."""
from dataclasses import asdict
import json
from uuid import uuid4

from plugins.symphony_pm import adapter as pm
from .owner_check_profile import check_plan_fingerprint
from .recovery import PROVEN_STOP
from .types import Refused, binding_from_json, finite, sha


def phase(owner, row):
    state = row['state']
    if state == 'LAUNCH_INTENT':
        return 'PENDING'
    if state in ('NOT_STARTED', 'HELD_UNKNOWN', 'RELEASED_BY_OWNER'):
        return state
    if state == 'STOPPED':
        if row['stop_proof'] not in PROVEN_STOP or not row['proof_source']:
            return 'HELD_UNKNOWN'
        result = json.loads(row['result']) if row['result'] else {}
        if (isinstance(result, dict) and type(result.get('exit_code')) is int and result['exit_code'] == 0
                and 'stop' in result and result['stop'] is None):
            return 'COMPLETED'
        return 'STOPPED'
    if not row['binding']:
        return 'HELD_UNKNOWN'
    binding = binding_from_json(json.loads(row['binding']))
    try:
        owner._refresh_observer()
        accounting = owner._job_accounting(binding)
    except Exception:  # health: allow BLE001 -- an unavailable or malformed observer cannot attest a running Job
        return 'HELD_UNKNOWN'
    return accounting if owner._coverage_verified() else 'HELD_UNKNOWN'


def sandbox_checks(owner, ticket):
    """Only the last capture and its bound CHECK can attest the owner-configured sandbox stage."""
    capture, check = owner.journal.stage_records(owner.token, ticket)
    patch = capture.get('patch') or {}
    settings = getattr(owner.launcher, 'settings', None)
    if (capture.get('source_sha') != ticket.plan.source_sha or not isinstance(patch, dict)
            or not sha(patch.get('sha256'), 64) or not isinstance(patch.get('paths'), list)
            or not patch['paths'] or not callable(getattr(settings, 'checks', None))):
        return (), ()
    try:
        plan = tuple(settings.checks(tuple(patch['paths'])))
        profile = settings.owner_check
        fingerprint = check_plan_fingerprint(profile, plan)
    except Exception:  # health: allow BLE001 -- status remains available when the owner cannot establish the plan
        return (), ()
    required = tuple(c.name for c in plan if not c.requires_git)
    deferred = [c.name for c in plan if c.requires_git]
    commands = check.get('commands')
    if (check.get('plan_sha256') != fingerprint or check.get('revisions') != profile.revisions()
            or not required or type(commands) is not dict or set(commands) != set(required)
            or check.get('deferred') != deferred or type(check.get('apply')) is not int or check['apply'] != 0
            or not ((not deferred and check.get('ok') is True and not check.get('reason'))
                or (deferred and check.get('ok') is False and check.get('reason') == 'CHECK_GIT_TESTS_DEFERRED'))):
        return required, ()
    return required, tuple((name, 'pass' if type(code) is int and code == 0 else 'fail')
                           for name, code in commands.items())


def observe(owner, ticket, row, *, request=None):
    state = phase(owner, row)
    required, checks = sandbox_checks(owner, ticket) if state in pm.DONE_PHASES else ((), ())
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
            'checks': checks, 'job_empty': row['state'] == 'STOPPED' and row['stop_proof'] == 'JOB_EMPTY'
                and bool(row['proof_source']), 'request_digest': request.request_digest if request else None}
    owner.journal.save_status(owner.token, ticket, body, seq)
    return body


def evidence(body):
    obs = pm.TrustedObservation(**body['observation'])
    target = (obs.producer, obs.evidence_ref, obs.job_ref, obs.job_revision)
    completions = (pm.TrustedCompletion(*target, obs.attempt_ref, True, tuple(map(tuple, body['checks']))),
                   ) if obs.phase in pm.DONE_PHASES else ()
    claims = tuple(pm.TrustedClaim(*target, claim) for claim in ('worker_stopped', 'descendants_stopped')
                   if body['job_empty'])
    return dict(trusted_producers=(obs.producer,), trusted_observations=(obs,), trusted_completions=completions,
                required_checks=tuple(body['required_checks']), trusted_claims=claims)
