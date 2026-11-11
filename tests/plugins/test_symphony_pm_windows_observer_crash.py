"""Owner crash while its observer holds a query-only Job duplicate; needs an approved native campaign.

Kill-on-close fires when the LAST Job handle closes, and a query-only handle counts. The observer's
duplicate lives in the owner process, so the owner's death must close both handles and the kernel
must stop the worker, its console host and its descendant. Kernel limits: 3 per worker Job, 6 per
parent containment Job. No test-side cleanup of the worker tree exists.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid

import pytest

from plugins.symphony_pm.transport.types import ProcessIdentity, Refused, binding_from_json
from tests.plugins.symphony_native_crash_support import (
    BoundedApi, NativeRig, helper_command, owner_environment, save_proof,
)
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('windows')
CUT = 'during_observer_read'


def _journal_rows(home):
    journal = next((home/'plugin-data'/'symphony_pm').iterdir())/'journal.sqlite3'
    with sqlite3.connect(f'file:{journal.as_posix()}?mode=ro', uri=True) as db:
        return (list(db.execute('SELECT * FROM serving_launches')), list(db.execute('SELECT * FROM effects')))


def _stopped(api, handle):
    try:
        wait_until(lambda: api.wait(handle), timeout=10, what='kernel stop')
    except AssertionError:
        return False
    return True


def test_owner_crash_during_observer_read_stops_worker_and_descendant(tmp_path):
    home = tmp_path/'profile'
    proofs = Path(os.environ.get('HERMES_E2E_ARTIFACTS', str(tmp_path)))
    proofs.mkdir(parents=True, exist_ok=True)
    receipt = proofs/('serving-'+CUT+'.json')
    helper = Path(__file__).with_name('symphony_native_crash_support.py').resolve()
    api, handles, owner = BoundedApi(6), {}, []
    containment = 'HermesTestContainment-'+uuid.uuid4().hex
    parent_job = api.create_job(containment)
    try:
        with (tmp_path/'serving.log').open('w+b') as log, tempfile.TemporaryFile(dir=tmp_path) as stdin:
            process, thread, pid, _ = api.create_suspended(
                [*helper_command(helper), str(home), CUT, str(receipt)], helper.parents[2],
                owner_environment(tmp_path, home, proofs), (stdin, log, log), parent_job, None)
            owner.extend((process, thread))
            api.resume(thread)
            wait_until(lambda: receipt.exists() or api.wait(process), timeout=30, what='native cut barrier')
            assert receipt.exists(), (tmp_path/'serving.log').read_text(encoding='utf-8', errors='replace')
            body = json.loads(receipt.read_text(encoding='utf-8'))
            assert body['serving']['process']['pid'] == pid and body['cut'] == CUT
            binding = binding_from_json(body['binding'])
            members = [ProcessIdentity(**value) for value in body['worker_roster']['members']]
            assert len(members) == 3 and binding.root in members
            for identity in members:
                handles[identity] = api.open_process(identity.pid)
                assert api.creation(handles[identity]) == identity.creation
                assert not api.wait(handles[identity])  # alive: the owner holds the Job and a duplicate
            receipt.with_suffix('.release').write_text('CUT', encoding='utf-8')
            assert _stopped(api, process) and api.exit_code(process) == 71
            # The parent containment Job is still open: only the dead owner's handles explain these stops.
            terminal = [{**asdict(identity), 'signaled': _stopped(api, handle)}
                        for identity, handle in handles.items()]
            save_proof('observer-read-crash', {'terminal': terminal, 'parent_containment_open': True,
                                               'orphan_cleanup': False, 'cancel': 'HOLD'})
            assert all(record['signaled'] for record in terminal)
            before = _journal_rows(home)
            assert len(before[0]) == 1 and before[0][0][5] == 'RESUME_INTENT' and before[1] == []
            with pytest.raises(Refused, match='OBSERVER_COVERAGE_UNKNOWN'):
                NativeRig(home, descendant=True)  # recovery holds; it never relaunches or releases
            assert _journal_rows(home) == before
            wait_until(lambda: api.snapshot(parent_job)[0] == 0, timeout=10, what='empty parent containment Job')
    finally:
        failures = []
        try:
            api.close_job(containment)
        except (OSError, Refused) as exc:
            failures.append(exc)
        for handle in (*handles.values(), *owner):
            try:
                api.close(handle)
            except OSError as exc:
                failures.append(exc)
        if failures:
            raise Refused('TEST_HANDLE_CLOSE_UNKNOWN') from failures[0]
