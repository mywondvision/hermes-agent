"""The worker Job has no kernel name; only a process-local registry can reach it.

Every kernel call is answered by a fake that hands out integers, so no real Job object,
handle or process exists in this file. Windows-only because open_job duplicates through
the real `_winapi` module, whose DuplicateHandle is replaced per test.
"""
import uuid
from types import SimpleNamespace

import pytest

from plugins.symphony_pm.transport import windows_api, windows_token
from plugins.symphony_pm.transport.types import ExecutionPlan, LaunchTicket, Refused, job_object_name
from plugins.symphony_pm.transport.windows_api import WindowsApi
from plugins.symphony_pm.transport.windows_receiver import WindowsReceiver

pytestmark = pytest.mark.platforms('windows')


OWNER_PROCESS, WORKER_TOKEN = 0x5000, 0x7001  # fake handle values; no OS object stands behind either


class FakeKernel(WindowsApi):
    """Fakes kernel32 (`fn`), advapi32 (`adv`) and the token boundary, so no real handle is touched."""

    def __init__(self, failing=()):
        self.calls, self.failing, self.next_handle = [], set(failing), 0x1000
        # process handle -> mandatory integrity RID (the owner is a medium-integrity user process);
        # a handle a test never listed raises KeyError instead of silently passing the boundary
        self.integrities = {OWNER_PROCESS: 0x2000}
        self.policies = {}  # process handle -> token mandatory policy; unlisted means no-write-up
        # what the kernel reports as the worker token's restricting SIDs; None means exactly the
        # list `worker_token` was asked to build
        self.restricting, self.attempt = None, None
        self.process_protected = self.directory_protected = True

    def fn(self, name, _args, _result):
        def call(*values):
            self.calls.append((name, values))
            if name in self.failing:
                return 0
            if name == 'CreateJobObjectW':
                self.next_handle += 1
                return self.next_handle
            return 1
        return call

    def adv(self, name, _args, _result):
        def call(*values):
            self.calls.append((name, values))
            return 0 if name in self.failing else 1
        return call

    def worker_token(self, attempt):
        self.calls.append(('worker_token', (attempt,)))
        self.attempt = attempt
        return WORKER_TOKEN

    def restricting_sids(self, _process):
        return windows_token.restricting_sids(self.attempt) if self.restricting is None else self.restricting

    def _current_process(self):
        return OWNER_PROCESS

    def integrity(self, process):
        return self.integrities[process]

    def mandatory_policy(self, process):
        return self.policies.get(process, 0x1)

    def protect_process(self):
        self.calls.append(('protect_process', ()))
        if isinstance(self.process_protected, BaseException):
            raise self.process_protected
        return self.process_protected

    def protect_directory(self, directory):
        self.calls.append(('protect_directory', (directory,)))
        return self.directory_protected

    def of(self, name):
        return [values for called, values in self.calls if called == name]

    @property
    def closed(self):
        return [values[0] for values in self.of('CloseHandle')]


class OtherKernel(FakeKernel):
    """A second class, as the observer's and the provider's WindowsApi instances are in production."""


@pytest.fixture(autouse=True)
def _registry_gains_no_entries():
    before = set(windows_api._JOBS)
    yield
    leaked = set(windows_api._JOBS) - before
    for job_id in leaked:  # fake handles only; entries that predate the test are never touched
        del windows_api._JOBS[job_id]
    assert not leaked


@pytest.fixture
def duplicates(monkeypatch):
    import _winapi
    calls = []

    def duplicate(*args):
        calls.append(args)
        return 0x2000 + len(calls)

    monkeypatch.setattr(_winapi, 'DuplicateHandle', duplicate)
    return calls


def _job_id():
    return 'job-'+uuid.uuid4().hex


def test_create_passes_no_kernel_name_and_registers_the_handle():
    api, job_id = FakeKernel(), _job_id()
    job = api.create_job(job_id)
    try:
        assert api.of('CreateJobObjectW') == [(None, None)]
        assert [values[1] for values in api.of('SetInformationJobObject')] == [9]
        limits = api.of('SetInformationJobObject')[0][2]._obj
        # kill-on-close plus the process and memory bounds, and no breakaway bit (0x800, 0x1000)
        assert limits.Basic.Flags == 0x2000 | 0x8 | 0x200 and not limits.Basic.Flags & 0x1800
        assert windows_api._JOBS[job_id] == job
    finally:
        api.close_job(job_id)


def test_second_create_with_the_same_id_is_refused_and_creates_nothing():
    api, job_id = FakeKernel(), _job_id()
    first = api.create_job(job_id)
    try:
        with pytest.raises(Refused, match='JOB_NAME_COLLISION'):
            api.create_job(job_id)
        assert len(api.of('CreateJobObjectW')) == 1 and api.closed == []
        assert windows_api._JOBS[job_id] == first
    finally:
        api.close_job(job_id)


def test_failed_limit_setup_closes_the_new_handle_and_registers_nothing():
    api, job_id = FakeKernel(failing=('SetInformationJobObject',)), _job_id()
    with pytest.raises(OSError):
        api.create_job(job_id)
    assert api.closed == [0x1001] and job_id not in windows_api._JOBS
    with pytest.raises(Refused, match='JOB_IDENTITY_UNKNOWN'):
        api.open_job(job_id)


def test_open_job_duplicates_with_query_access_only(duplicates):
    import _winapi
    api, job_id = FakeKernel(), _job_id()
    job = api.create_job(job_id)
    try:
        query = api.open_job(job_id)
        current = _winapi.GetCurrentProcess()
        assert duplicates == [(current, job, current, 4, False, 0)]
        assert query != job and api.closed == []
    finally:
        api.close_job(job_id)


def test_open_job_for_an_unknown_id_is_refused_as_in_a_successor_process(duplicates):
    with pytest.raises(Refused, match='JOB_IDENTITY_UNKNOWN'):
        FakeKernel().open_job(_job_id())
    assert duplicates == []


def test_close_job_closes_the_handle_once_and_forgets_the_id(duplicates):
    api, job_id = FakeKernel(), _job_id()
    job = api.create_job(job_id)
    api.close_job(job_id)
    assert api.closed == [job]
    with pytest.raises(Refused, match='JOB_IDENTITY_UNKNOWN'):
        api.open_job(job_id)
    with pytest.raises(Refused, match='JOB_IDENTITY_UNKNOWN'):
        api.close_job(job_id)
    assert api.closed == [job] and duplicates == []


def test_every_instance_and_subclass_sees_the_same_registry(duplicates):
    creator, observer, job_id = FakeKernel(), OtherKernel(), _job_id()
    creator.create_job(job_id)
    try:
        assert observer.open_job(job_id) and len(duplicates) == 1
        with pytest.raises(Refused, match='JOB_NAME_COLLISION'):
            observer.create_job(job_id)
    finally:
        observer.close_job(job_id)
    with pytest.raises(Refused, match='JOB_IDENTITY_UNKNOWN'):
        creator.open_job(job_id)


def _receiver(api, tmp_path):
    plan = ExecutionPlan('fixture-model', 'high', 'fixture-environment', str(tmp_path), 'a'*40)
    ticket = LaunchTicket('fixture', 'project', 'job', '0'*64, 'attempt-'+uuid.uuid4().hex,
                          '0'*64, 'permit', plan, 'session')
    return ticket, lambda: WindowsReceiver(
        api, ticket, ['fixture'], tmp_path, '', environment={},
        command_result=lambda *args, **kw: SimpleNamespace(), sanitize=lambda text: text,
        private_directory=tmp_path, data_directory=tmp_path/'plugin-data')


def test_receiver_that_fails_after_create_releases_its_registered_job(tmp_path):
    class FailsToStart(FakeKernel):
        def create_suspended(self, *_args):
            raise Refused('FIXTURE_CREATE_FAILED')

    api = FailsToStart()
    ticket, build = _receiver(api, tmp_path)
    with pytest.raises(Refused, match='FIXTURE_CREATE_FAILED'):
        build()
    # the token is closed as soon as create returns or fails; the Job last, via the registry
    assert api.closed == [WORKER_TOKEN, 0x1001] and job_object_name(ticket) not in windows_api._JOBS


def test_receiver_whose_job_cannot_be_set_up_leaves_no_entry_and_no_open_handle(tmp_path):
    api = FakeKernel(failing=('SetInformationJobObject',))
    ticket, build = _receiver(api, tmp_path)
    with pytest.raises(OSError):
        build()
    assert api.closed == [0x1001] and job_object_name(ticket) not in windows_api._JOBS


def test_receiver_refused_for_a_taken_id_never_closes_the_other_owner(tmp_path):
    api = FakeKernel()
    ticket, build = _receiver(api, tmp_path)
    owner = api.create_job(job_object_name(ticket))
    try:
        with pytest.raises(Refused, match='JOB_NAME_COLLISION'):
            build()
        assert api.closed == [] and windows_api._JOBS[job_object_name(ticket)] == owner
    finally:
        api.close_job(job_object_name(ticket))
