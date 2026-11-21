"""The operator qualification stand exchanges ordinary synthetic gateway messages offline."""
import re

from tests.plugins.symphony_typed_human_support import human_session


def test_two_scoped_plans_through_local_adapter(tmp_path):
    with human_session(tmp_path) as channel:
        assert channel.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}
        for action in ('approve','deny'):
            state, shown = channel.request(action)
            assert state == 'WAITING_FOR_AUTHORITY'
            assert 'Record the '+action+' fixture decision.' in shown
            command, = re.findall(r'/symphony '+action+r' [a-zA-Z0-9_-]+', shown)
            reply = channel.deliver(command)
            assert reply.split('.')[0] == ('APPROVED' if action == 'approve' else 'DENIED')
            state = channel.retry(action)
            assert state == ('SUBMITTED' if action == 'approve' else 'WAITING_FOR_AUTHORITY')
        assert channel.counts() == {'permits': 1, 'reservations': 1, 'launcher_calls': 1}


def test_standalone_profile_can_open_without_pytest_fixtures(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys

    env = {k: os.environ[k] for k in ('SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'COMSPEC', 'PATH', 'PATHEXT',
           'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'PROCESSOR_ARCHITECTURE',
           'NUMBER_OF_PROCESSORS', 'OS') if k in os.environ}
    for var, folder in {'HOME': 'user', 'USERPROFILE': 'user', 'APPDATA': 'appdata', 'LOCALAPPDATA': 'local',
                        'TMP': 'temp', 'TEMP': 'temp', 'HERMES_RUNTIME_DIR': 'runtime'}.items():
        path = tmp_path / folder
        path.mkdir(exist_ok=True)
        env[var] = str(path)
    env['HERMES_HOME'] = str(tmp_path / 'profile')
    scenario = '''
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from tests.plugins.symphony_typed_human_support import human_session
with human_session(Path(sys.argv[1])) as session:
    if any(session.counts().values()):
        raise ValueError('expected a fresh fixture journal')
'''
    subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', scenario, str(tmp_path)],
                   cwd=Path(__file__).resolve().parents[2], env=env, stdin=subprocess.DEVNULL,
                   check=True, timeout=60)


def test_standalone_cli_has_no_owner_decision_command(tmp_path):
    import argparse
    from types import SimpleNamespace
    import pytest
    from hermes_cli.main import _attach_plugin_cli_command
    from plugins.symphony_pm.status_cli import register

    with human_session(tmp_path) as channel:
        _, plan = channel.request('approve')
        code, = re.findall(r'/symphony approve ([a-f0-9]{16})', plan)
        parser = argparse.ArgumentParser()
        register(SimpleNamespace(register_cli_command=lambda **entry: _attach_plugin_cli_command(
            parser.add_subparsers(), entry)))
        for action in ('approve', 'deny'):
            with pytest.raises(SystemExit) as failure:
                parser.parse_args(['symphony', action, code])
            assert failure.value.code == 2
        assert channel.counts() == {'permits': 0, 'reservations': 0, 'launcher_calls': 0}
