"""A worker is never born with the owner's own token, and a boundary that cannot be verified refuses.

Fixture-only: every kernel and advapi32 call is answered by `FakeKernel`, so no token, Job or
process exists. The real `TokenBoundary.require_boundary` runs against faked integrity levels and
faked restricting SIDs, so the decision itself is what is exercised. Windows-only because `WindowsApi` is built on the real
Windows module types.
"""
import sqlite3

import pytest

from plugins.symphony_pm.transport import windows_api
from plugins.symphony_pm.transport import windows_token
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
        def worker_token(self, attempt):
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


def _refused_before_any_worker(api, tmp_path):
    ticket, build = _receiver(api, tmp_path)
    with pytest.raises(Refused, match='WORKER_BOUNDARY_UNENFORCED'):
        build()
    # the owner's side fails before a Job, a token or a process exists for the worker
    assert api.created == [] and api.closed == [] and api.of('worker_token') == []
    assert api.of('CreateJobObjectW') == [] and job_object_name(ticket) not in windows_api._JOBS


@pytest.mark.parametrize('owner_integrity', [LOW, HIGH])
def test_owner_that_is_not_exactly_medium_is_refused_before_any_worker_exists(tmp_path, owner_integrity):
    # a low owner gives no boundary; an elevated one would leave the administrator group in the worker
    _refused_before_any_worker(LaunchingKernel(owner_integrity=owner_integrity), tmp_path)


def test_owner_process_that_cannot_be_labelled_is_refused_before_any_worker_exists(tmp_path):
    api = LaunchingKernel()
    api.process_protected = False
    _refused_before_any_worker(api, tmp_path)
    assert api.of('protect_directory') == []


def test_operating_system_error_while_labelling_is_a_refusal_not_a_crash(tmp_path):
    api = LaunchingKernel()
    api.process_protected = OSError('fixture: label cannot be set')
    _refused_before_any_worker(api, tmp_path)


def test_plugin_data_that_cannot_be_labelled_is_refused_before_any_worker_exists(tmp_path):
    api = LaunchingKernel()
    api.directory_protected = False
    _refused_before_any_worker(api, tmp_path)
    assert api.of('protect_directory') == [(tmp_path/'plugin-data',)]


def test_low_worker_whose_token_forbids_writing_up_passes_the_boundary():
    api = LaunchingKernel(worker_integrity=LOW)
    api.restricting = windows_token.restricting_sids('attempt-1')
    api.require_boundary(WORKER_PROCESS, 'attempt-1')


def test_worker_token_is_built_for_exactly_this_attempt(tmp_path):
    api = LaunchingKernel()
    ticket, build = _receiver(api, tmp_path)
    receiver = build()
    try:
        assert api.of('worker_token') == [(ticket.attempt,)]
    finally:
        receiver.close_fail_safe(0)


def test_each_attempt_has_its_own_stable_sid_outside_the_user_and_logon_authorities():
    first, second = windows_token.attempt_sid('attempt-1'), windows_token.attempt_sid('attempt-2')
    assert first == windows_token.attempt_sid('attempt-1') != second
    for sid in (first, second):
        assert sid.startswith('S-1-9-') and len(sid.split('-')) == 7
    listed = windows_token.restricting_sids('attempt-1')
    assert listed[0] == first and second not in listed
    # neither a user account (S-1-5-21-...) nor a logon session (S-1-5-5-...) is ever listed
    assert not [sid for sid in listed if sid.startswith(('S-1-5-21-', 'S-1-5-5-'))]


@pytest.mark.parametrize('reported', [
    lambda attempt: (),  # an ordinary token: no restricting check at all
    lambda attempt: windows_token.SHARED_SIDS,  # restricted, but not to this attempt
    lambda attempt: windows_token.restricting_sids('another-attempt'),
    lambda attempt: (*windows_token.restricting_sids(attempt), 'S-1-5-21-1-2-3-1001'),  # the user is listed
    lambda attempt: (*windows_token.restricting_sids(attempt), 'S-1-5-5-0-4242'),  # the logon session is listed
], ids=['unrestricted', 'no-attempt-sid', 'other-attempt', 'user-sid', 'logon-sid'])
def test_worker_whose_restricting_sids_are_not_exactly_this_attempts_is_refused(tmp_path, reported):
    api = LaunchingKernel()
    ticket, build = _receiver(api, tmp_path)
    api.restricting = reported(ticket.attempt)
    with pytest.raises(Refused, match='WORKER_BOUNDARY_UNENFORCED'):
        build()
    assert sorted(api.closed) == sorted([WORKER_TOKEN, 0x1001, WORKER_THREAD, WORKER_PROCESS])
    assert job_object_name(ticket) not in windows_api._JOBS
    assert api.of('ResumeThread') == []


def test_low_worker_whose_token_allows_writing_up_is_refused(tmp_path):
    api = LaunchingKernel()
    api.policies[WORKER_PROCESS] = 0x2  # new-process-min only: integrity alone would not stop a write
    _, build = _receiver(api, tmp_path)
    with pytest.raises(Refused, match='WORKER_BOUNDARY_UNENFORCED'):
        build()
    assert sorted(api.closed) == sorted([WORKER_TOKEN, 0x1001, WORKER_THREAD, WORKER_PROCESS])
    assert api.of('ResumeThread') == []


def test_production_job_bounds_the_process_count_and_memory_of_one_worker_tree():
    api = LaunchingKernel()
    api.create_job('fixture-limits')
    try:
        (_, kind, limits, _), = api.of('SetInformationJobObject')
        basic = limits._obj.Basic
        assert kind == 9 and basic.Flags & 0x2000 and basic.Flags & 0x8 and basic.Flags & 0x200
        assert basic.ActiveLimit == api.process_limit > 0 and limits._obj.JobMemory == api.memory_limit > 0
    finally:
        api.close_job('fixture-limits')


def test_production_never_creates_a_process_with_the_callers_own_token():
    with pytest.raises(Refused, match='WORKER_TOKEN_REQUIRED'):
        windows_api.WindowsApi._spawn(LaunchingKernel(), None, [], ())


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
