"""Project the verified admission/plan/attempt chain on the private OPERATOR route."""

import json

from gateway.session_desktop_operator_store import deny

from .desktop_operator_stop import stop_operation
from .transport.journal import permit_from_json


def attempt_for(approval, live_route, admission_id):
    owner = approval.owner
    journal = owner.journal
    path = journal.path.with_name('typed-plan-audit.sqlite3')
    if not path.is_file():
        return None
    with journal._plan_audit(owner.token, None, approval.writer) as conn:
        tables = {row[0] for row in conn.execute('SELECT name FROM plan_audit.sqlite_master')}
        if not {'voice_inputs', 'desktop_plan_decisions'} <= tables:
            return None
        origin = conn.execute('SELECT * FROM plan_audit.voice_inputs WHERE route=? AND message_id=?',
                              (live_route, admission_id)).fetchone()
        if origin is None:
            return None
        decision = conn.execute(
            'SELECT receipt FROM plan_audit.desktop_plan_decisions WHERE namespace=? AND session_id=? AND plan_digest=?',
            (journal.namespace, origin['session_id'], origin['plan_digest']),
        ).fetchone()
        if decision is None:
            return None
        receipt = json.loads(decision['receipt'])
        if (receipt['decision'] != 'APPROVED' or receipt['request_id'] != origin['request_id']
                or receipt['plan_digest'] != origin['plan_digest'] or receipt['profile'] != journal.home):
            return None
        matches = conn.execute('''SELECT l.project,l.job,j.revision,p.body,p.binding FROM serving_launches l
            JOIN jobs j USING(namespace,project,job)
            JOIN serving_permits p ON p.ref=json_extract(l.ticket,'$.permit_ref')
            WHERE l.namespace=? AND p.used=1 AND json_extract(p.body,'$.contract_digest')=?''',
            (journal.namespace, origin['plan_digest']),).fetchall()
        if len(matches) != 1:
            return None
        match, = matches
        permit = permit_from_json(json.loads(match['body']))
        ticket, _ = journal._launch(conn, match['project'], match['job'], match['revision'])
        if (permit.binding_digest != match['binding'] or permit.identity.request_id != origin['request_id']
                or permit.identity.session_id != ticket.session or ticket.session != origin['session_id']
                or permit.identity.profile_home != journal.home or permit.identity.namespace != journal.namespace
                or permit.plan != ticket.plan or permit.request_digest != ticket.request_digest):
            return None
        return dict(attempt_id=ticket.attempt, request_id=origin['request_id'], plan_digest=origin['plan_digest'],
                    project=ticket.project, job=ticket.job, revision=ticket.revision)


def attempt_operation(approval, db, ref, live_route, admission_id, proof, payload, current, commit_guard):
    action = proof.fields[2]
    attempt = attempt_for(approval, live_route, admission_id)
    if action == 'receipt':
        return attempt
    if attempt is None or payload['attempt_id'] != attempt['attempt_id']:
        deny()
    owner = approval.owner
    target = attempt['project'], attempt['job'], attempt['revision']
    if action in ('apply_info', 'apply', 'archive', 'apply_result'):
        from .desktop_operator_apply import apply_operation

        return apply_operation(approval, db, ref, attempt, proof, payload, current, commit_guard)
    if action == 'status':
        with db._read_ctx() as conn:
            current(conn)
        view = owner.observe_status(*target)
        if view['attempt_ref'] != attempt['attempt_id']:
            deny()
        return dict(attempt_id=attempt['attempt_id'], phase=view['phase'], observed_at=view['observed_at'],
                    evidence=view['evidence_ref'])
    return stop_operation(owner, db, ref, proof, payload, current, commit_guard, target)
