"""A worker is never born with the owner's own token, and a boundary that cannot be verified refuses.

Fixture-only: every kernel and advapi32 call is answered by `FakeKernel`, so no token, Job or
process exists. The real `TokenBoundary.require_boundary` runs against faked integrity levels, so
the decision itself is what is exercised. Windows-only because `WindowsApi` is built on the real
Windows module types.
"""
import sqlite3

import pytest

from plugins.symphony_pm.transport import windows_api
from plugins.symphony_pm.transport.lifecycle import AttemptRunner
from plugins.symphony_pm.transport.types import Refused, job_object_name
from plugins.symphony_pm.transport.windows_token import LOW, MEDIUM
from tests.plugins.symphony_serving_support import approve, rig
from tests.plugins.test_symphony_pm_job_registry import (  # the autouse registry fixture must be in scope here
    OWNER_PROCESS, WORKER_TOKEN, FakeKernel, _receiver, _registry_gains_no_entries,
)
from tests.tools.test_symphony_pm_adapter import submit_payload

pytestmark = pytest.mark.platforms('windows')

HIGH = 0x3000
WORKER_PROCESS, WORKER_THREAD = 0x3001, 0x3002


class LaunchingKernel(FakeKernel):
    """Hands out a fake worker the way `create_suspended` does, and records what it was given."""

    def __init__(self, worker_integrity=LOW, owner_integrity=MEDIUM, **kwargs):
        super().__init__(**kwargs)
        self.integrities.update({WORKER_PROCESS: worker_integrity, OWNER_PROCESS: owner_integrity})
        self.created, self.create_failure = [], None

    def create_suspended(self, argv, cwd, env, streams, job, token):
        self.created.append({'job': job, 'token': token})
        if self.create_failure:
            raise self.create_failure
        return WORKER_PROCESS, WORKER_THREAD, 4242, 1


def test_worker_is_created_with_the_restricted_token_which_is_closed_once(tmp_path):
    api = LaunchingKernel()
    _, build = _receiver(api, tmp_path)
    receiver = build()
    try:
        assert [call['token'] for call in api.created] == [WORKER_TOKEN]
        assert api.closed == [WORKER_TOKEN]
        assert api.of('ResumeThread') == []  # the boundary check precedes any resume
    finally:
        receiver.close_fail_safe(0)


def test_failed_create_still_closes_the_token_once_and_releases_the_job(tmp_path):
    api = LaunchingKernel()
    api.create_failure = Refused('FIXTURE_CREATE_FAILED')
    ticket, build = _receiver(api, tmp_path)
    with pytest.raises(Refused, match='FIXTURE_CREATE_FAILED'):
        build()
    assert api.closed == [WORKER_TOKEN, 0x1001]
    assert job_object_name(ticket) not in windows_api._JOBS


def test_token_that_cannot_be_built_refuses_without_creating_a_worker(tmp_path):
    class NoToken(LaunchingKernel):
        def worker_token(self):
            raise OSError('fixture: token cannot be restricted')

    api = NoToken()
    ticket, build = _receiver(api, tmp_path)
    with pytest.raises(OSError, match='token cannot be restricted'):
        build()
    assert api.created == []  # in particular, no create with the owner's own token
    assert api.closed == [0x1001] and job_object_name(ticket) not in windows_api._JOBS


def test_worker_at_the_owners_integrity_is_refused_and_everything_is_released(tmp_path):
    api = LaunchingKernel(worker_integrity=MEDIUM, owner_integrity=MEDIUM)
    ticket, build = _receiver(api, tmp_path)
    with pytest.raises(Refused, match='WORKER_BOUNDARY_UNENFORCED'):
        build()
    assert sorted(api.closed) == sorted([WORKER_TOKEN, 0x1001, WORKER_THREAD, WORKER_PROCESS])
    assert job_object_name(ticket) not in windows_api._JOBS
    assert api.of('ResumeThread') == []


def test_low_integrity_owner_gives_the_worker_no_boundary(tmp_path):
    api = LaunchingKernel(worker_integrity=LOW, owner_integrity=LOW)
    ticket, build = _receiver(api, tmp_path)
    with pytest.raises(Refused, match='WORKER_BOUNDARY_UNENFORCED'):
        build()
    assert sorted(api.closed) == sorted([WORKER_TOKEN, 0x1001, WORKER_THREAD, WORKER_PROCESS])
    assert job_object_name(ticket) not in windows_api._JOBS
    assert api.of('ResumeThread') == []


def test_low_worker_below_a_medium_owner_passes_the_boundary():
    LaunchingKernel(worker_integrity=LOW, owner_integrity=MEDIUM).require_boundary(WORKER_PROCESS)


def test_elevated_owner_is_refused_because_its_worker_would_keep_the_administrator_group():
    with pytest.raises(Refused, match='WORKER_BOUNDARY_UNENFORCED'):
        LaunchingKernel(worker_integrity=LOW, owner_integrity=HIGH).require_boundary(WORKER_PROCESS)


def test_boundary_failure_in_the_serving_lifecycle_spends_the_slot_and_holds_the_namespace(
        tmp_path, monkeypatch):
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)
        ticket, _, _ = r.owner.journal.reserve_launch(r.owner.token, request, context, r.now[0])
        entered = []

        def unenforced(*_args):
            entered.append(1)
            raise Refused('WORKER_BOUNDARY_UNENFORCED')

        monkeypatch.setattr(r.native, 'create_suspended', unenforced)
        run = AttemptRunner(r.owner, r.owner.token, ticket)
        argv = ['fixture', 'exec']
        with pytest.raises(Refused, match='WORKER_BOUNDARY_UNENFORCED'):
            run(argv, cwd=ticket.plan.workspace, timeout=1, stdin_text='fixture')
        _, row = r.owner.journal.read_launch(r.owner.token, ticket.project, ticket.job, ticket.revision)
        assert row['state'] == 'CREATE_INTENT' and row['binding'] is None

        with pytest.raises(Refused, match='LAUNCH_TICKET_SPENT'):
            run(argv, cwd=ticket.plan.workspace, timeout=1, stdin_text='fixture')
        other, other_context, _ = approve(r, payload=submit_payload(request_id='request-2'))
        with pytest.raises(Refused, match='UNSETTLED_PRIOR_LAUNCH_HOLD'):
            r.owner.submit(other, other_context)

        assert entered == [1] and r.native.created == []
        with sqlite3.connect(r.owner.journal.path) as db:
            used = dict(db.execute('SELECT ref, used FROM serving_permits'))
        assert used['permit-'+other.request_id] == 0
    finally:
        r.owner.close(0)
