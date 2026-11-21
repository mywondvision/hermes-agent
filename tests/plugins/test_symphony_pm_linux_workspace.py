"""Stopped workspace fixtures exercise Git export, sealing, archive identity and recapture."""
from dataclasses import replace
from pathlib import Path
import os
import subprocess
from types import SimpleNamespace

import pytest

from plugins.symphony_pm.transport import linux_workspace as ws
from plugins.symphony_pm.transport.types import Refused
from tests.plugins.test_symphony_pm_linux_lifecycle import rig, submit


def test_same_archive_digest_recapture_and_refuse_in_place_change(tmp_path_factory, monkeypatch):
    # Git for Windows must resolve full loose-object names in the nested private indexes.
    tmp_path = tmp_path_factory.mktemp('lx')
    r = rig(tmp_path/'profile')
    t = submit(r, 'one')
    repo = tmp_path/'source'
    repo.mkdir()
    def git(*args):
        return subprocess.run([ws._git(), '-C', str(repo), *args], check=True, capture_output=True).stdout
    git('init', '-q')
    (repo/'task.txt').write_bytes(b'base\n')
    git('add', 'task.txt')
    git('-c', 'user.email=test@invalid', '-c', 'user.name=Fixture', 'commit', '-q', '-m', 'base')
    sha = git('rev-parse', 'HEAD').decode('ascii').strip()
    fixture = replace(t, plan=replace(t.plan, source_sha=sha))
    attempts = tmp_path/'attempts'
    active = attempts/'active'
    active.mkdir(parents=True)
    launcher = SimpleNamespace(settings=SimpleNamespace(active=active, attempts_root=attempts, repository=repo),
                               qualification=SimpleNamespace(namespace=t.namespace, profile_home=r.scope.profile_home))
    monkeypatch.setattr(ws, '_mountpoints', lambda: set())
    monkeypatch.setattr(ws, '_sync_directory', lambda path: None)
    _, workspace, capture = ws.prepare(launcher, fixture)
    (workspace.src/'task.txt').write_bytes(b'first\n')
    archive = attempts/fixture.attempt
    active.rename(archive)
    ws.seal_workspace(launcher, fixture, archive)
    capture.workspace = replace(workspace, path=archive)
    first = ws.capture_patch(capture)
    assert Path(first['path']).is_file() and first['paths'] == ['task.txt']
    assert (repo/'task.txt').read_bytes() == b'base\n'
    primary = Path(first['path']).read_bytes()
    class Journal:
        def __init__(self):
            self.observations = []
        def proven_stop(self, row):
            return True
        def next_observation_seq(self, token, ticket):
            return len(self.observations) + 5
        def append_observation(self, token, ticket, observation, *, expected_seq):
            assert expected_seq == self.next_observation_seq(token, ticket)
            self.observations.append(observation)
    journal = Journal()
    def again():
        return ws.recapture(launcher, journal, r.owner.token, fixture, {'hold_reason': None})
    a, b = again(), again()
    assert (a['seq'], b['seq']) == (5, 6)
    assert a['patch']['sha256'] == b['patch']['sha256'] == first['sha256']
    assert a['patch']['path'] != b['patch']['path']
    (archive/'src'/'task.txt').write_bytes(b'later\n')
    with pytest.raises(Refused, match='ARCHIVE_CHANGED'):
        again()
    assert len(journal.observations) == 2 and Path(first['path']).read_bytes() == primary


def test_preflight_rejects_mount_and_hardlink_before_hashing(tmp_path):
    source = tmp_path/'src'
    source.mkdir()
    child = source/'copied'
    child.mkdir()
    with pytest.raises(Refused, match='MOUNT'):
        ws._validate_tree(source, {}, {child})
    original = tmp_path/'owner-file'
    original.write_bytes(b'fixture')
    os.link(original, child/'linked')
    with pytest.raises(Refused, match='WORKSPACE_TYPE'):
        ws._validate_tree(source, {}, set())


@pytest.mark.platforms('linux')
def test_native_file_types_and_modes_are_refused_before_read(tmp_path):
    source = tmp_path/'src'
    source.mkdir()
    fifo = source/'fifo'
    os.mkfifo(fifo)
    with pytest.raises(Refused, match='TYPE'):
        ws._validate_tree(source, {}, ws._mountpoints())
    fifo.unlink()
    script = source/'task'
    script.write_bytes(b'x')
    script.chmod(0o755)
    with pytest.raises(Refused, match='MODE'):
        ws._validate_tree(source, {}, ws._mountpoints())
