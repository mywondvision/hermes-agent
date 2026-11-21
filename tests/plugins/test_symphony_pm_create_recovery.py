"""Fenced recovery from qualified predecessor evidence, without repeating a create."""
import ctypes as C
import sqlite3

import pytest

from plugins.symphony_pm.transport.journal import Journal
from plugins.symphony_pm.transport.lifecycle import AttemptRunner
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.symphony_serving_support import approve, rig
from tests.plugins.test_symphony_pm_adapter import submit_payload

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


def _row(r, ticket):
    return r.owner.journal.read_launch(r.owner.token, ticket.project, ticket.job, ticket.revision)[1]


@pytest.mark.parametrize(('entered', 'expected'), [(True, 'STOPPED'), (False, 'NOT_STARTED')])
def test_proven_successor_preserves_previous_ticket_and_admits_new_grant(tmp_path, monkeypatch, entered, expected):
    ticket = _predecessor(tmp_path, monkeypatch, create_entered=entered)
    path = Journal(tmp_path, 'symphony_pm:ingress').path
    before = _reservation(path)
    r = _successor(tmp_path)
    try:
        row = _row(r, ticket)
        assert row['state'] == expected
        assert row['stop_proof'] == ('OWNER_DEAD' if entered else None)
        assert row['proof_source'] == 'fixture-readback'
        assert row['result'] is None and row['binding'] is None
        after = _reservation(path)
        for table in ('jobs', 'operations', 'effects', 'serving_permits'):
            assert after[table] == before[table]
        assert r.owner.journal.unfinished(r.owner.token) == []
        with pytest.raises(Refused, match='CREATE_SLOT_SPENT'):
            r.owner.journal.claim_create(r.owner.token, ticket)
        request, context, _ = approve(r)
        assert r.owner.submit(request, context) == (ticket, False)
        assert r.native.created == []
        request, context, _ = approve(r, payload=submit_payload(request_id='request-2'))
        new, fresh = r.owner.submit(request, context)
        assert fresh and new != ticket and len(r.native.created) == 1
    finally:
        r.owner.close(0)


def test_same_boot_live_predecessor_keeps_epoch_and_launch(tmp_path, monkeypatch):
    ticket = _predecessor(tmp_path, monkeypatch, create_entered=True)
    path = Journal(tmp_path, ticket.namespace).path
    before = _reservation(path)
    with pytest.raises(Refused, match='PREDECESSOR_LIVE_OR_UNKNOWN'):
        rig(tmp_path, incarnation='rival')
    assert _reservation(path) == before


def test_foreign_guard_cannot_claim_copied_namespace(tmp_path, monkeypatch):
    from tests.plugins.symphony_serving_support import Guard
    ticket = _predecessor(tmp_path, monkeypatch, create_entered=True)
    path = Journal(tmp_path, ticket.namespace).path
    before = _reservation(path)
    monkeypatch.setattr(Guard, 'recovery_host_id', 'another-fixture-host')
    with pytest.raises(Refused, match='HOST_IDENTITY_CHANGED'):
        _successor(tmp_path)
    assert _reservation(path) == before


def test_unattested_execution_remains_held_across_claims_until_owner_release(tmp_path, monkeypatch):
    from tests.plugins.symphony_serving_support import Native
    monkeypatch.setattr(Native, 'ownership_contract', None)
    ticket = _predecessor(tmp_path, monkeypatch, create_entered=True)
    first = _successor(tmp_path)
    assert _row(first, ticket)['state'] == 'HELD_UNKNOWN'
    first.owner.close(0)
    r = _successor(tmp_path, incarnation='third')
    try:
        row = _row(r, ticket)
        assert row['state'] == 'HELD_UNKNOWN' and row['stop_proof'] is None and row['hold_reason']
        request, context, _ = approve(r, payload=submit_payload(request_id='new-grant'))
        with pytest.raises(Refused, match='UNSETTLED_PRIOR'):
            r.owner.submit(request, context)
        r.owner.journal.release_by_owner(r.owner.token, ticket, 'fixture operator decision')
        assert _row(r, ticket)['state'] == 'RELEASED_BY_OWNER'
        assert r.owner.submit(request, context)[1]
    finally:
        r.owner.close(0)


def test_bound_held_predecessor_remains_releasable_without_its_job_handle(tmp_path, monkeypatch):
    from tests.plugins.symphony_serving_support import Native, Observer

    monkeypatch.setattr(Native, 'ownership_contract', None)
    first = rig(tmp_path)
    request, context, _ = approve(first)
    ticket, _ = first.owner.submit(request, context)
    first.owner.close(0)
    handshake = Observer.handshake

    def without_old_job(observer, owner, generation, targets):
        if any(target.binding.ticket == ticket for target in targets):
            raise Refused('JOB_HANDLE_UNAVAILABLE')
        return handshake(observer, owner, generation, targets)

    monkeypatch.setattr(Observer, 'handshake', without_old_job)
    successor = _successor(tmp_path)
    try:
        assert _row(successor, ticket)['state'] == 'HELD_UNKNOWN'
        request, context, _ = approve(successor, payload=submit_payload(request_id='after-hold'))
        with pytest.raises(Refused, match='OBSERVER_COVERAGE_UNKNOWN'):
            successor.owner.submit(request, context)
        assert not successor.native.created
        with pytest.raises(Refused, match='WRITER_FENCED'):
            successor.owner.release_by_owner(None, ticket.project, ticket.job, ticket.revision, 'test')
        successor.owner.release_by_owner(successor.owner.token, ticket.project, ticket.job, ticket.revision,
                                        'operator checked the original attempt')
        assert _row(successor, ticket)['state'] == 'RELEASED_BY_OWNER'
        assert successor.owner.submit(request, context)[1]
    finally:
        successor.owner.close(0)


def test_new_boot_on_same_fixture_host_has_distinct_evidence(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.plugins.symphony_serving_support import Guard
    ticket = _predecessor(tmp_path, monkeypatch, create_entered=False)
    original = Guard.__init__
    def next_boot(self, *args):
        original(self, *args)
        self.identity = replace(self.identity, boot_id='next-boot')
    monkeypatch.setattr(Guard, '__init__', next_boot)
    r = rig(tmp_path, incarnation='new-boot-owner')
    try:
        row = _row(r, ticket)
        assert (row['state'], row['stop_proof']) == ('STOPPED', 'BOOT_CHANGED')
    finally:
        r.owner.close(0)


def test_create_slot_and_not_started_have_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)
        journal, token = r.owner.journal, r.owner.token
        ticket, _, _ = journal.reserve_launch(token, request, context, r.now[0])
        barrier = Barrier(2)
        def attempt(operation):
            barrier.wait(timeout=20)
            try:
                return operation(token, ticket) is not False
            except Refused as exc:
                assert str(exc) in ('CREATE_SLOT_SPENT', 'STORE_OUTCOME_UNKNOWN')
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = list(pool.map(attempt, [journal.claim_create, journal.mark_not_started]))
        assert sum(winners) == 1
        assert _row(r, ticket)['state'] == ('CREATE_INTENT' if winners[0] else 'NOT_STARTED')
    finally:
        r.owner.close(0)


@pytest.mark.platforms('windows')
def test_attribute_list_carries_handle_and_job_lists_without_creating_a_process():
    from plugins.symphony_pm.transport.windows_api import StartupInfoExW, StartupInfoW, WindowsApi
    assert C.sizeof(StartupInfoExW) == C.sizeof(StartupInfoW) + C.sizeof(C.c_void_p)
    with WindowsApi().attribute_list([0x1004, 0x1008, 0x100C], 0x1010) as attributes:
        assert C.sizeof(attributes) > 0
