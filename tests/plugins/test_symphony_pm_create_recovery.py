"""A successor owner records, never repairs, a create the dead predecessor left unbound."""
import ctypes as C
import json
import sqlite3

import pytest

from plugins.symphony_pm.transport.journal import Journal
from plugins.symphony_pm.transport.lifecycle import AttemptRunner
from plugins.symphony_pm.transport.types import Refused, job_object_name
from tests.plugins.symphony_serving_support import approve, rig
from tests.tools.test_symphony_pm_adapter import submit_payload

ARGV = ['fixture', 'exec']


def _predecessor(tmp_path, monkeypatch, *, create_entered):
    first = rig(tmp_path)
    request, context, _ = approve(first)
    ticket, _, _ = first.owner.journal.reserve_launch(first.owner.token, request, context, first.now[0])
    if create_entered:
        def uncertain(*_args):
            raise Refused('UNCERTAIN_CUT')

        monkeypatch.setattr(first.native, 'create_suspended', uncertain)
        with pytest.raises(Refused, match='UNCERTAIN_CUT'):
            AttemptRunner(first.owner, first.owner.token, ticket)(
                ARGV, cwd=ticket.plan.workspace, timeout=1, stdin_text='fixture')
    first.owner.close(0)
    return ticket


def _successor(tmp_path, incarnation='incarnation-2'):
    return rig(tmp_path, incarnation=incarnation, predecessor_dead=True)


def _reservation(path):
    tables = ('serving_launches', 'serving_permits', 'jobs', 'operations', 'effects')
    with sqlite3.connect(path) as db:
        return {name: list(db.execute(f'SELECT * FROM {name}')) for name in tables}


def _record(r, ticket):
    row = r.owner.journal.reconcile_record(r.owner.token, ticket)
    return row and json.loads(row['body'])


def test_unbound_create_is_recorded_as_unobserved_and_launch_stays_held(tmp_path, monkeypatch):
    ticket = _predecessor(tmp_path, monkeypatch, create_entered=True)
    r = _successor(tmp_path)
    try:
        assert _record(r, ticket) == {'state': 'CREATE_INTENT', 'job_object': job_object_name(ticket),
                                      'outcome': 'CREATE_UNBOUND_UNOBSERVED'}
        assert r.native.created == []
        _, row = r.owner.journal.read_launch(r.owner.token, ticket.project, ticket.job, ticket.revision)
        assert row['state'] == 'CREATE_INTENT' and row['binding'] is None and row['result'] is None
    finally:
        r.owner.close(0)


def test_reconciliation_changes_no_launch_permit_job_operation_or_effect(tmp_path, monkeypatch):
    ticket = _predecessor(tmp_path, monkeypatch, create_entered=True)
    path = Journal(tmp_path, 'symphony_pm:ingress').path
    expected = _reservation(path)
    r = _successor(tmp_path)
    try:
        assert _record(r, ticket)['outcome'] == 'CREATE_UNBOUND_UNOBSERVED'
        assert _reservation(path) == expected
        assert expected['serving_permits'][0][-1] == 1
    finally:
        r.owner.close(0)


def test_successor_cannot_create_again_or_start_another_launch(tmp_path, monkeypatch):
    ticket = _predecessor(tmp_path, monkeypatch, create_entered=True)
    r = _successor(tmp_path)
    try:
        with pytest.raises(Refused, match='CREATE_SLOT_SPENT'):
            r.owner.journal.claim_create(r.owner.token, ticket)
        with pytest.raises(Refused, match='LAUNCH_TICKET_SPENT'):
            AttemptRunner(r.owner, r.owner.token, ticket)(
                ARGV, cwd=ticket.plan.workspace, timeout=1, stdin_text='fixture')
        other, other_context, _ = approve(r, payload=submit_payload(request_id='request-2'))
        with pytest.raises(Refused, match='UNSETTLED_PRIOR_LAUNCH_HOLD'):
            r.owner.submit(other, other_context)
        assert r.native.created == []
        with sqlite3.connect(r.owner.journal.path) as db:
            used = dict(db.execute('SELECT ref, used FROM serving_permits'))
        assert used['permit-'+other.request_id] == 0 and sum(used.values()) == 1
    finally:
        r.owner.close(0)


def test_launch_that_never_entered_the_os_is_recorded_as_such(tmp_path, monkeypatch):
    ticket = _predecessor(tmp_path, monkeypatch, create_entered=False)
    r = _successor(tmp_path)
    try:
        assert _record(r, ticket) == {'state': 'LAUNCH_INTENT', 'job_object': job_object_name(ticket),
                                      'outcome': 'NEVER_ENTERED_OS'}
        request, context, _ = approve(r)
        replay, fresh = r.owner.submit(request, context)
        assert replay == ticket and not fresh and r.native.created == []
    finally:
        r.owner.close(0)


def test_unwritable_reconcile_record_never_yields_an_owner(tmp_path, monkeypatch):
    ticket = _predecessor(tmp_path, monkeypatch, create_entered=True)

    def unavailable(*_args):
        raise Refused('STORE_OUTCOME_UNKNOWN')

    with monkeypatch.context() as patch:
        patch.setattr(Journal, 'record_reconcile', unavailable)
        with pytest.raises(Refused, match='STORE_OUTCOME_UNKNOWN'):
            _successor(tmp_path)
    r = _successor(tmp_path, incarnation='incarnation-3')
    try:
        assert _record(r, ticket)['outcome'] == 'CREATE_UNBOUND_UNOBSERVED' and r.native.created == []
    finally:
        r.owner.close(0)


def test_a_later_successor_keeps_the_first_observation(tmp_path, monkeypatch):
    ticket = _predecessor(tmp_path, monkeypatch, create_entered=True)
    first = _successor(tmp_path)
    try:
        recorded = first.owner.journal.reconcile_record(first.owner.token, ticket)
    finally:
        first.owner.close(0)
    second = _successor(tmp_path, incarnation='incarnation-3')
    try:
        assert second.owner.token.epoch > recorded['writer_epoch']
        assert second.owner.journal.reconcile_record(second.owner.token, ticket) == recorded
    finally:
        second.owner.close(0)


@pytest.mark.platforms('windows')
def test_attribute_list_carries_handle_and_job_lists_without_creating_a_process():
    from plugins.symphony_pm.transport.windows_api import StartupInfoExW, StartupInfoW, WindowsApi
    assert C.sizeof(StartupInfoExW) == C.sizeof(StartupInfoW) + C.sizeof(C.c_void_p)
    with WindowsApi().attribute_list([0x1004, 0x1008, 0x100C], 0x1010) as attributes:
        assert C.sizeof(attributes) > 0
