"""Attempt workspace, patch capture and the read boundary, without starting any worker.

Capture and policy run real Git against temporary repositories; the read-boundary decisions run
against the fake kernel of `test_symphony_pm_job_registry`, so no token, Job or process exists.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest

from hermes_platform.resolver import locate_command
from plugins.symphony_pm.transport import windows_token
from plugins.symphony_pm.transport.attempt_workspace import (
    Capture, Workspace, export, require_patch_policy, require_plain_tree,
)
from plugins.symphony_pm.transport.types import Refused
from plugins.symphony_pm.transport.windows_read_boundary import (
    FILE, KEY, DenyEntries, has_class_deny, with_class_deny, without_class_deny,
)
from plugins.symphony_pm.transport.windows_receiver import require_plain_environment

GIT = (locate_command('git').command or [shutil.which('git')])[0]
SDDL = 'O:BAG:BAD:PAI(A;OICI;FA;;;BA)(A;OICIID;0x1200a9;;;BU)'


def _git(repository, *args):
    return subprocess.run([GIT, '-C', str(repository), '-c', 'user.name=t', '-c', 'user.email=t@invalid',
                           '-c', 'core.autocrlf=false', *args], check=True, capture_output=True).stdout


@pytest.fixture
def source(tmp_path):
    repository = tmp_path/'repository'
    repository.mkdir()
    _git(repository, 'init', '-q')
    (repository/'README.md').write_bytes(b'hello\n')
    (repository/'gone.txt').write_bytes(b'remove me\n')
    (repository/'.gitattributes').write_bytes(b'*.py text eol=lf\n')
    (repository/'pkg').mkdir()
    (repository/'pkg'/'module.py').write_bytes(b'x = 1\n')
    _git(repository, 'add', '-A')
    _git(repository, 'commit', '-q', '-m', 'base')
    return repository, _git(repository, 'rev-parse', 'HEAD').decode().strip()


def _workspace(tmp_path, repository, sha):
    workspace = Workspace('attempt-'+uuid.uuid4().hex[:8], tmp_path/'attempts'/'one')
    workspace.src.mkdir(parents=True)
    export(GIT, repository, sha, workspace.src)
    capture = Capture(GIT, repository, sha, workspace, tmp_path/'owner'/'shadow', tmp_path/'owner'/'results')
    capture.baseline()
    return workspace, capture


# --- copy and patch ---------------------------------------------------------------------------

def test_copy_has_no_git_and_its_baseline_is_the_source_tree(tmp_path, source):
    repository, sha = source
    workspace, capture = _workspace(tmp_path, repository, sha)
    assert not (workspace.src/'.git').exists() and (workspace.src/'pkg'/'module.py').read_bytes() == b'x = 1\n'
    assert capture.base == capture.source_tree


def test_patch_carries_exactly_the_changes_and_applies_to_the_commit(tmp_path, source):
    repository, sha = source
    workspace, capture = _workspace(tmp_path, repository, sha)
    (workspace.src/'README.md').write_bytes(b'hello\nworld\n')
    (workspace.src/'gone.txt').unlink()
    (workspace.src/'new.txt').write_bytes(b'new\n')
    result = capture.result()
    assert result.paths == ('README.md', 'gone.txt', 'new.txt')
    manifest = json.loads((tmp_path/'owner'/'results'/(workspace.attempt+'.json')).read_text(encoding='utf-8'))
    assert manifest['sha256'] == result.sha256 and manifest['paths'] == list(result.paths)
    # Independent of the capture: the same patch applies to a plain checkout of the commit.
    check = tmp_path/'check'
    _git(repository, 'worktree', 'add', '-q', '--detach', str(check), sha)
    _git(check, 'apply', '--binary', str(result.patch))
    assert (check/'README.md').read_bytes() == b'hello\nworld\n' and not (check/'gone.txt').exists()


def test_a_file_the_worker_cannot_hide_with_an_ignore_rule(tmp_path, source):
    repository, sha = source
    workspace, capture = _workspace(tmp_path, repository, sha)
    (workspace.src/'hidden.txt').write_bytes(b'not hidden\n')
    (workspace.src/'.gitignore').write_bytes(b'hidden.txt\n')
    with pytest.raises(Refused, match='ATTEMPT_POLICY'):  # the ignore file is itself a Git file
        capture.result()
    (workspace.src/'.gitignore').unlink()
    (Path(capture.results)/(workspace.attempt+'.patch')).unlink(missing_ok=True)
    assert 'hidden.txt' in capture.result().paths


@pytest.mark.parametrize('name', ['.gitattributes', '.gitmodules', 'pkg/.gitignore', '.github/workflows/x.yml'])
def test_changes_to_git_files_are_refused(tmp_path, source, name):
    repository, sha = source
    workspace, capture = _workspace(tmp_path, repository, sha)
    target = workspace.src/name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'[filter "x"]\n')
    with pytest.raises(Refused, match='ATTEMPT_POLICY'):
        capture.result()


def test_a_nested_git_directory_is_refused_before_git_reads_it(tmp_path, source):
    repository, sha = source
    workspace, capture = _workspace(tmp_path, repository, sha)
    (workspace.src/'.git'/'hooks').mkdir(parents=True)
    (workspace.src/'.git'/'hooks'/'pre-commit').write_bytes(b'#!/bin/sh\n')
    with pytest.raises(Refused, match='ATTEMPT_POLICY'):
        capture.result()
    assert not (tmp_path/'owner'/'results').exists()  # refused before any patch was written


def test_a_hard_link_is_refused(tmp_path, source):
    repository, sha = source
    workspace, capture = _workspace(tmp_path, repository, sha)
    outside = tmp_path/'outside.txt'
    outside.write_bytes(b'outside\n')
    os.link(outside, workspace.src/'linked.txt')
    with pytest.raises(Refused, match='ATTEMPT_HARDLINK'):
        capture.result()


@pytest.mark.platforms('windows')
def test_a_junction_is_refused_without_reading_through_it(tmp_path, source):
    import _winapi
    repository, sha = source
    workspace, capture = _workspace(tmp_path, repository, sha)
    outside = tmp_path/'outside'
    outside.mkdir()
    (outside/'canary.txt').write_bytes(b'canary\n')
    _winapi.CreateJunction(str(outside), str(workspace.src/'link'))
    with pytest.raises(Refused, match='ATTEMPT_REPARSE_POINT'):
        capture.result()


def test_patch_policy_refuses_mode_changes_links_and_size():
    for header in (b'old mode 100644\nnew mode 100755', b'new file mode 120000', b'new file mode 160000'):
        with pytest.raises(Refused, match='ATTEMPT_POLICY'):
            require_patch_policy(b'diff --git a/x b/x\n'+header+b'\n', ('x',))
    with pytest.raises(Refused, match='ATTEMPT_PATCH_LIMIT'):
        require_patch_policy(b'x'*11, ('x',), limit=10)
    require_patch_policy(b'diff --git a/x b/x\n+ old mode 100644\n', ('x',))  # content, not a header


def test_plain_tree_walk_accepts_ordinary_files(tmp_path):
    (tmp_path/'a'/'b').mkdir(parents=True)
    (tmp_path/'a'/'b'/'c.txt').write_bytes(b'c')
    require_plain_tree(tmp_path)


def test_a_capture_without_baseline_is_refused(tmp_path, source):
    repository, sha = source
    workspace = Workspace('attempt-x', tmp_path/'w')
    workspace.src.mkdir(parents=True)
    capture = Capture(GIT, repository, sha, workspace, tmp_path/'shadow', tmp_path/'results')
    with pytest.raises(Refused, match='ATTEMPT_BASELINE_MISSING'):
        capture.result()


def test_workspace_environment_keeps_home_and_temp_inside_the_attempt(tmp_path):
    workspace = Workspace('a', tmp_path/'a')
    environment = workspace.environment({'SYSTEMROOT': 'C:/Windows'})
    for name in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP'):
        assert Path(environment[name]).is_relative_to(workspace.path)
    assert environment['SYSTEMROOT'] == 'C:/Windows'


@pytest.mark.parametrize('name', ['OPENAI_API_KEY', 'GITHUB_TOKEN', 'DB_PASSWORD', 'AWS_SECRET_ACCESS_KEY', 'MY_AUTH'])
def test_environment_names_that_look_secret_are_refused(name):
    with pytest.raises(Refused, match='WORKER_ENVIRONMENT_SECRET'):
        require_plain_environment({'SYSTEMROOT': 'C:/Windows', name: 'x'})
    require_plain_environment({'SYSTEMROOT': 'C:/Windows', 'TEMP': 'x', 'HOME': 'y'})


# --- deny entries: SDDL and the reversible journal ---------------------------------------------

def test_class_deny_goes_first_and_is_removed_exactly():
    added = with_class_deny(SDDL, FILE)
    assert has_class_deny(added) and not has_class_deny(SDDL)
    assert added.index(windows_token.WORKER_CLASS_SID) < added.index('(A;OICI;FA;;;BA)')
    assert with_class_deny(added, FILE) == added
    assert without_class_deny(added) == SDDL
    inherit_only = SDDL.replace('D:PAI', f'D:PAI(D;OICIIO;0x1200a9;;;{windows_token.WORKER_CLASS_SID})')
    assert not has_class_deny(inherit_only)  # it would not apply to the object itself


class FakeObjects:
    def __init__(self, objects):
        self.sddl, self.writes = dict(objects), []

    def object_sddl(self, kind, target):
        return self.sddl[(kind, target)]

    def write_dacl(self, kind, target, sddl):
        self.writes.append((kind, target))
        self.sddl[(kind, target)] = sddl


def test_apply_and_revert_are_idempotent_and_restore_the_snapshot(tmp_path):
    objects = {(FILE, 'D:/decoy'): SDDL, (KEY, 'CURRENT_USER\\Software\\Decoy'): 'O:SYG:SYD:P(A;;KR;;;RC)'}
    api = FakeObjects(objects)
    entries = DenyEntries(api, tmp_path/'journal.json')
    entries.apply(list(objects))
    entries.apply(list(objects))
    assert len(api.writes) == 2 and all(has_class_deny(value) for value in api.sddl.values())
    states = json.loads((tmp_path/'journal.json').read_text(encoding='utf-8'))
    assert {entry['state'] for entry in states.values()} == {'applied'}
    # an interrupted run is finished from the journal alone, by a new instance
    DenyEntries(api, tmp_path/'journal.json').revert()
    DenyEntries(api, tmp_path/'journal.json').revert()
    assert api.sddl == objects


def test_revert_reports_drift_instead_of_writing_the_snapshot_back(tmp_path):
    api = FakeObjects({(FILE, 'D:/decoy'): SDDL})
    entries = DenyEntries(api, tmp_path/'journal.json')
    entries.apply([(FILE, 'D:/decoy')])
    api.sddl[(FILE, 'D:/decoy')] += '(A;;FA;;;SY)'  # somebody else changed the DACL meanwhile
    with pytest.raises(Refused, match='DACL_DRIFT_SINCE_SNAPSHOT'):
        entries.revert()
    assert not has_class_deny(api.sddl[(FILE, 'D:/decoy')]) and api.sddl[(FILE, 'D:/decoy')].endswith('(A;;FA;;;SY)')


def test_an_entry_that_does_not_stick_is_reported(tmp_path):
    class Ignores(FakeObjects):
        def write_dacl(self, kind, target, sddl):
            self.writes.append((kind, target))

    with pytest.raises(Refused, match='DENY_ENTRY_NOT_APPLIED'):
        DenyEntries(Ignores({(FILE, 'D:/decoy'): SDDL}), tmp_path/'journal.json').apply([(FILE, 'D:/decoy')])
