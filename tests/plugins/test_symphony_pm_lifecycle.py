"""Injected receiver state machine: fixture counters are not OS/process acceptance."""
from dataclasses import replace

import pytest

from plugins.symphony_pm.transport.lifecycle import AttemptRunner
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.symphony_serving_support import approve, rig


def test_bound_resume_and_duplicate_submit_create_only_one_receiver(tmp_path):
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)
        ticket, fresh = r.owner.submit(request, context)
        again, duplicate = r.owner.submit(request, context)
        assert again == ticket and fresh and not duplicate
        assert len(r.native.created) == 1 and r.native.created[0].resumes == 1
        _, row = r.owner.journal.read_launch(r.owner.token, ticket.project, ticket.job, ticket.revision)
        assert row['state'] == 'RESUME_INTENT' and row['binding'] and row['result']
    finally:
        r.owner.close(0)


def test_pm_timeout_cleanup_share_one_effect_even_when_ack_lost(tmp_path):
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)
        r.owner.submit(request, context)
        receiver = r.native.created[0]
        receiver.fail_cancel = True
        for cause in ('PM_CANCEL', 'TIMEOUT', 'EMERGENCY_CLEANUP'):
            result = r.owner.cancel_internal(receiver.binding, cause)
            assert result.hold and not result.local_stopped
        effect = r.owner.journal.effect(r.owner.token, receiver.binding)
        assert receiver.cancels == 1 and effect['state'] == 'UNKNOWN'
        assert effect['dispatch_epoch'] == r.owner.token.epoch and effect['proof'] is None
    finally:
        r.owner.close(0)


def test_timeout_drains_and_does_not_allow_cleanup_to_dispatch_twice(tmp_path):
    r = rig(tmp_path)
    r.native.timeout = True
    try:
        request, context, _ = approve(r)
        r.owner.submit(request, context)
        receiver = r.native.created[0]
        assert receiver.waits == 2 and receiver.cancels == 1
        r.owner.cancel_internal(receiver.binding, 'EMERGENCY_CLEANUP')
        assert receiver.cancels == 1
    finally:
        r.owner.close(0)


def test_recovery_never_creates_receiver_or_restores_spent_slot(tmp_path):
    first = rig(tmp_path)
    request, context, _ = approve(first)
    first.owner.submit(request, context)
    binding = first.native.created[0].binding
    first.owner.cancel_internal(binding, 'PM_CANCEL')
    first.owner.close(0)
    recovered = rig(tmp_path, incarnation='incarnation-2', predecessor_dead=True)
    try:
        result = recovered.owner.cancel_internal(binding, 'TIMEOUT')
        assert result.state == 'UNKNOWN' and recovered.native.created == []
        row = recovered.owner.journal.effect(recovered.owner.token, binding)
        assert row['dispatch_epoch'] == 1 and row['state'] == 'CANCEL_REQUESTED'
    finally:
        recovered.owner.close(0)


def test_host_admission_close_prevents_native_entry(tmp_path):
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)
        ticket, _, _ = r.owner.journal.reserve_launch(r.owner.token, request, context, r.now[0])
        runner = AttemptRunner(r.owner, r.owner.token, ticket)
        r.scope.active = False
        with pytest.raises(Refused, match='WRITER_FENCED'):
            runner(['fixture', 'exec'], cwd=ticket.plan.workspace, timeout=1, stdin_text='fixture')
        assert r.native.created == []
    finally:
        r.owner.close(0)


def test_query_only_local_stop_retains_hold_and_does_not_release_reservation(tmp_path):
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)
        r.owner.submit(request, context)
        receiver = r.native.created[0]
        r.observer.stopped = True
        result = r.owner.cancel_internal(receiver.binding, 'PM_CANCEL')
        assert result.state == 'LOCAL_STOPPED_HOLD' and result.hold and result.local_stopped
        assert result.reservation == 'POSSIBLY_CONSUMED_HELD' and receiver.cancels == 0
        wrong = replace(receiver.binding, job_object='foreign-job')
        with pytest.raises(Refused, match='CANCEL_TARGET_BINDING'):
            r.owner.cancel_internal(wrong, 'TIMEOUT')
    finally:
        r.owner.close(0)


def test_storage_cut_after_create_closes_owned_receiver_without_resume_or_retry(tmp_path, monkeypatch):
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)

        def unavailable(*_args):
            raise Refused('STORE_OUTCOME_UNKNOWN')

        monkeypatch.setattr(r.owner.journal, 'bind_native', unavailable)
        with pytest.raises(Refused, match='STORE_OUTCOME_UNKNOWN'):
            r.owner.submit(request, context)
        receiver = r.native.created[0]
        assert receiver.closes == 1 and receiver.resumes == receiver.cancels == 0
        _, fresh = r.owner.submit(request, context)
        assert not fresh and len(r.native.created) == 1
    finally:
        # The unbound durable intent remains uncertain even after owned handle
        # close; cleanup must report HOLD rather than manufacture cancel proof.
        with pytest.raises(Refused, match='OWNED_CLEANUP_INCOMPLETE'):
            r.owner.close(0)
