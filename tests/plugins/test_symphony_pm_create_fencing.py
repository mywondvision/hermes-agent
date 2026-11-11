"""Uncertain native entry spends the durable slot; no real processes are started."""
import pytest

from plugins.symphony_pm.transport.lifecycle import AttemptRunner
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.symphony_serving_support import approve, rig


@pytest.mark.parametrize('cut', ['create', 'bind'])
def test_retained_runner_cannot_repeat_uncertain_create(tmp_path, monkeypatch, cut):
    r = rig(tmp_path)
    request, context, _ = approve(r)
    ticket, _, _ = r.owner.journal.reserve_launch(r.owner.token, request, context, r.now[0])
    runner = AttemptRunner(r.owner, r.owner.token, ticket)
    calls = []

    def fail(*args):
        calls.append(args)
        raise Refused('UNCERTAIN_CUT')

    monkeypatch.setattr(r.native if cut == 'create' else r.owner.journal,
                        'create_suspended' if cut == 'create' else 'bind_native', fail)
    with pytest.raises(Refused, match='UNCERTAIN_CUT'):
        runner(['fixture', 'exec'], cwd=ticket.plan.workspace, timeout=1, stdin_text='fixture')
    with pytest.raises(Refused, match='LAUNCH_TICKET_SPENT'):
        runner(['fixture', 'exec'], cwd=ticket.plan.workspace, timeout=1, stdin_text='fixture')
    assert len(calls) == 1
    _, row = r.owner.journal.read_launch(r.owner.token, ticket.project, ticket.job, ticket.revision)
    assert row['state'] == 'CREATE_INTENT' and row['binding'] is None
    if cut == 'bind':
        with pytest.raises(Refused, match='OWNED_CLEANUP_INCOMPLETE'):
            r.owner.close(0)
    else:
        r.owner.close(0)
