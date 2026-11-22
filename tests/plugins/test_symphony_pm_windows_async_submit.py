"""CR-5 A5. Observe the serving port's submit and independent cancel on the qualified CR-4 stand."""
from dataclasses import asdict
import json
import sqlite3
import time

import pytest

from hermes_cli.plugin_request_binding import RequestIdentity
from plugins.symphony_pm import adapter as pm
from plugins.symphony_pm.transport.codex_exec_launcher import CONFIG_SELECTED
from plugins.symphony_pm.transport.types import ExecutionPlan, Permit
from plugins.symphony_pm.transport.windows_api import WindowsApi
from tests.plugins.symphony_native_crash_support import save_proof
from tests.plugins.test_symphony_pm_adapter import ctx, submit_payload
from tests.plugins.test_symphony_pm_run_supervisor import cancel_request
from tests.plugins.test_symphony_pm_windows_job_stop import _attempt, _available_memory, _stand, _witness
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')


def _submit(stand):
    payload = submit_payload(request_id='cr5-async-submit')
    request, _, _ = pm.parse_intent(payload)
    identity = RequestIdentity(stand.scope.profile_home, stand.scope.namespace, 'session-1', request.request_id,
                               request.payload_digest)
    plan = ExecutionPlan(CONFIG_SELECTED, CONFIG_SELECTED, 'fixture-client', str(stand.attempts/'active'/'src'),
                         stand.source)
    now = time.time()
    permit = Permit('cr5-grant', identity, request.project_ref, request.request_digest,
                    'b'*64, 'policy', 'scope', 'issuer', now+600, plan)
    owner = stand.owner
    owner.record_permit(owner.token, permit, 'fixture-approval', 'c'*64, now)
    context = ctx(payload, issuer_ref=permit.issuer_ref, policy_ref=permit.policy_ref, grant_ref=permit.ref,
                  approved_scope_revision=permit.scope_revision, contract_digest=permit.contract_digest,
                  scheduler_id=owner.scheduler_id, scheduler_epoch=owner.token.epoch, now=now, expires_at=permit.expires_at)
    return stand.port.submit_intent(request, context), permit


def test_returned_job_can_be_canceled_from_a_separate_authorized_call(tmp_path, monkeypatch):
    stand = _stand(tmp_path)
    owner = stand.owner
    api, handles, records = WindowsApi(), {}, []
    src = stand.attempts/'active'/'src'
    proof = {'model_runs': 0, 'events': {}, 'members': records, 'resource': _available_memory('async-submit')}
    monkeypatch.setattr(stand.launcher, '_attempt', lambda _, ticket, request, runner, active:
                        _attempt(stand, 'cancel', ticket, runner, active))
    monkeypatch.setattr(stand.launcher, 'job_coverage_for', lambda guard:
                        guard is owner.guard and stand.qualified_ticket is not None
                        and stand.qualified_ticket.attempt in owner._receivers and stand.independent_stop())
    try:
        receipt, permit = _submit(stand)
        proof['events']['return'] = time.time()
        assert receipt.outcome == 'SUBMITTED'
        ticket, _ = owner.journal.read_launch(owner.token, permit.project_ref, receipt.job_ref, receipt.job_revision)
        proof.update(job=ticket.job, revision=ticket.revision, attempt=ticket.attempt)
        read = lambda: owner.journal.read_launch(owner.token, ticket.project, ticket.job, ticket.revision)[1]
        wait_until(lambda: (src/'child.json').is_file() or read()['result'], timeout=30, what='CR-4 child barrier')
        assert (src/'child.json').is_file(), read()['state']
        with owner.lock:
            receiver, = owner._receivers.values()
            binding = receiver.binding
            assert binding.ticket == ticket
            before = proof['before'] = _witness(api, binding, src, handles, records)
            assert not before['errors'] and before['active'] == len(before['pids']) and records
            assert all(r.get('expected_identity') and r.get('alive') and r.get('listed') and r.get('in_job')
                       for r in records)
            stand.qualified_ticket = ticket
            stand.independent_stop = lambda: all(api.wait(h) for h in handles.values())
        control = cancel_request(owner, permit, receipt, time.time())
        proof['events']['cancel'] = time.time()
        reply = stand.port.request_cancel(*control)
        assert reply.job_ref == receipt.job_ref and reply.outcome == 'CANCEL_REQUESTED'
        wait_until(lambda: read()['result'], timeout=40, what='durable stop and capture')
        row = read()
        assert row['state'] == 'STOPPED' and row['stop_proof'] == 'JOB_EMPTY' and not row['hold_reason']
        assert json.loads(row['result'])['workspace']['renamed'] and not (stand.attempts/'active').exists()
        with sqlite3.connect(owner.journal.path.as_uri()+'?mode=ro', uri=True) as db:
            raw, = db.execute('SELECT body FROM serving_readbacks WHERE effect_key=?', (binding.effect_key,)).fetchone()
        final = json.loads(raw)
        assert final['binding']['ticket'] == asdict(ticket)
        assert final['job_active'] == 0 and final['job_members'] == [] and stand.independent_stop()
        proof.update(state=row['state'], stop_proof=row['stop_proof'], final=final, cancel=reply.outcome)
        proof['events']['observed_stop'] = final['observed_at']
        assert proof['events']['return'] <= proof['events']['cancel'] <= proof['events']['observed_stop']
    finally:
        try:
            stand.close(time.monotonic()+10)
        finally:
            for handle in handles.values():
                api.close(handle)
            save_proof('cr5-windows-async-submit', proof)
