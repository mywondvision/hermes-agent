"""Owner recovery with a fixture client, real Git patch and durable SQLite observations."""
import json
from pathlib import Path
import shutil

import pytest

from plugins.symphony_pm.transport.lifecycle import LifecycleOwner
from plugins.symphony_pm.transport.recapture import attempt_data
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.symphony_serving_support import Guard, Observer
from tests.plugins.test_symphony_pm_codex_exec import rig, submit

pytestmark = pytest.mark.platforms('windows')  # fixture transport holds a real Windows executable handle


@pytest.fixture
def recovered(tmp_path):
    r = rig(tmp_path)
    ticket, _ = submit(r)
    r.owner.guard.close(0)
    owner = LifecycleOwner(r.scope, Guard(r.scope, 'next-owner', predecessor_dead=True), Observer(r.now),
                           r.native, r.launcher, scheduler_id='serving-test', controller='next-controller',
                           clock=lambda: r.now[0])
    try:
        yield r, owner, ticket
    finally:
        owner.close(0)


def test_recapture_numbers_new_patches_and_keeps_first_result(recovered):
    r, owner, ticket = recovered
    identity = (ticket.project, ticket.job, ticket.revision)
    original = owner.journal.read_launch(owner.token, *identity)[1]['result']
    patch = json.loads(original)['patch']
    next_seq = owner.journal.next_observation_seq(owner.token, ticket)
    for invalid_token in (None, r.owner.token):
        with pytest.raises(Refused, match='WRITER_FENCED'):
            owner.recapture(invalid_token, *identity)
    first = owner.recapture(owner.token, *identity)
    second = owner.recapture(owner.token, *identity)
    assert (first['seq'], second['seq']) == (next_seq, next_seq + 1)
    assert first['patch']['sha256'] == second['patch']['sha256'] == patch['sha256']
    primary, one, two = (Path(x['path']) for x in (patch, first['patch'], second['patch']))
    assert (one.name, two.name) == (f'capture-{next_seq}.patch', f'capture-{next_seq + 1}.patch')
    assert primary.parent != one.parent != two.parent
    assert primary.read_bytes() == one.read_bytes() == two.read_bytes()
    (r.attempts/ticket.attempt/'src'/'pkg'/'value.py').write_bytes(b'VALUE = 1\n')
    empty = owner.recapture(owner.token, *identity)
    assert empty['seq'] == next_seq + 2 and empty['patch']['paths'] == []
    assert Path(empty['patch']['path']).read_bytes() == b''
    assert owner.journal.read_launch(owner.token, *identity)[1]['result'] == original


def test_recapture_requires_the_original_measured_baseline(recovered):
    r, owner, ticket = recovered
    identity = (ticket.project, ticket.job, ticket.revision)
    next_seq = owner.journal.next_observation_seq(owner.token, ticket)
    shadow = attempt_data(r.launcher.qualification, ticket)/'shadow'
    (shadow/'baseline.index').unlink()
    with pytest.raises(Refused, match='ATTEMPT_BASELINE_MISSING'):
        owner.recapture(owner.token, *identity)
    shutil.copyfile(shadow/'after.index', shadow/'baseline.index')
    with pytest.raises(Refused, match='ATTEMPT_BASELINE_MISMATCH'):
        owner.recapture(owner.token, *identity)
    assert owner.journal.next_observation_seq(owner.token, ticket) == next_seq


def test_recapture_checks_provider_and_archive_before_append(recovered, monkeypatch):
    r, owner, ticket = recovered
    next_seq = owner.journal.next_observation_seq(owner.token, ticket)
    with monkeypatch.context() as m:
        m.setattr(owner, 'launcher', lambda *args: None)
        with pytest.raises(Refused, match='RECAPTURE_UNAVAILABLE'):
            owner.recapture(owner.token, ticket.project, ticket.job, ticket.revision)
    original = r.attempts/ticket.attempt
    kept = r.attempts/'kept'
    original.rename(kept)
    shutil.copytree(kept, original)
    with pytest.raises(Refused, match='ATTEMPT_WORKSPACE_IDENTITY'):
        owner.recapture(owner.token, ticket.project, ticket.job, ticket.revision)
    assert owner.journal.next_observation_seq(owner.token, ticket) == next_seq


def test_workspace_recovery_requires_the_recorded_directory(tmp_path):
    r = rig(tmp_path)
    try:
        ticket, _ = submit(r)
        archive = r.attempts/ticket.attempt
        active = r.launcher.settings.active
        active.mkdir()
        assert not r.launcher.workspace_clear()
        assert not r.launcher.settle_workspace(ticket)
        assert archive.is_dir() and active.is_dir()
        active.rmdir()
        archive.rename(active)
        assert r.launcher.settle_workspace(ticket)
        assert r.launcher.workspace_clear() and archive.is_dir()
    finally:
        r.owner.close(0)
