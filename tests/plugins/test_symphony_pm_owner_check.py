"""The owner keeps a captured patch and records CHECK even when its separate MXC profile is unavailable."""
import json
import os
from pathlib import Path

import pytest

from tests.plugins.test_symphony_pm_codex_exec import launches, rig, submit
from tests.plugins.symphony_owner_check_support import check_plan, owner_client
from plugins.symphony_pm.transport.owner_check_profile import CheckCommand, PROFILE

pytestmark = pytest.mark.platforms('windows')


def test_unqualified_owner_check_records_refusal_and_retains_capture(tmp_path):
    r = rig(tmp_path)
    submit(r)
    [row] = launches(r)
    result = json.loads(row['result'])
    assert result['check']['ok'] is False
    assert result['check']['reason'] == 'CHECK_PROFILE_MISSING'
    assert result['check']['commands'] == {}
    assert result['check']['patch_sha256'] == result['patch']['sha256']


@pytest.mark.parametrize('refused', [True, False])
def test_owner_routes_only_git_and_mxc_with_explicit_environments(tmp_path, monkeypatch, refused):
    monkeypatch.setenv('UNRELATED_CONTROL_FIELD', 'synthetic')
    r = rig(tmp_path)

    def plan(paths):
        return (*check_plan(paths), CheckCommand('git-test', 'python', ('-m', 'pytest', 'git_test.py'), True))

    with owner_client(r, plan=plan, refusal=refused) as (spawns, q):
        ticket, _ = submit(r)
    [row] = launches(r)
    result = json.loads(row['result'])
    check = result['check']
    assert Path(result['patch']['path']).is_file()
    assert check['ok'] is False and check['deferred'] == ['git-test']
    assert check['reason'] == ('CHECK_FAILED_PermissionError' if refused else 'CHECK_GIT_TESTS_DEFERRED')
    assert check['commands'] == ({} if refused else {'value': 0})
    view = r.owner.observe_status(ticket.project, ticket.job, ticket.revision)
    assert view['stage_acceptance'] == ('NOT_ACCEPTED' if refused else 'ACCEPTED')
    git_names = {k for k in ('SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'TEMP', 'TMP') if k in os.environ}
    git_names.update(('GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_TERMINAL_PROMPT'))
    client_calls = []
    for argv, names in spawns:
        if Path(argv[0]) == q.git:
            expected = set(git_names)
            if 'checkout-index' in argv or '-c' in argv:
                expected.update(('GIT_DIR', 'GIT_WORK_TREE', 'GIT_ATTR_SOURCE', 'GIT_INDEX_FILE'))
            elif 'apply' in argv:
                expected.add('GIT_CEILING_DIRECTORIES')
            assert names == expected
        elif argv[:2] == [str(q.client), 'sandbox']:
            client_calls.append(argv)
            expected = {'SYSTEMROOT', 'SYSTEMDRIVE', 'PATH', 'CODEX_HOME', 'HOME', 'USERPROFILE',
                        'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP'}
            if 'WINDIR' in os.environ:
                expected.add('WINDIR')
            assert names == expected
        else:
            # The separately qualified, synthetic model precheck is outside the owner-check profile.
            assert argv[1:3] == [str(r.attempts.parent/'fake_codex.py'), 'sandbox']
    [call] = client_calls
    assert call[:7] == [str(q.client), 'sandbox', '-C', str(q.anchor), '-P', PROFILE, '--']
    assert 'git_test.py' not in call
