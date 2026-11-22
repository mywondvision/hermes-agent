"""Windows project terminal acceptance, through Hermes' normal tool dispatch in a fresh profile."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest


def sha(path):
    with path.open('rb') as reader:
        return hashlib.file_digest(reader, 'sha256').hexdigest()


@pytest.mark.platforms('windows')
def test_native_project_terminal(tmp_path, monkeypatch):
    from hermes_cli.plugins_loader import _plugin_home_scope
    from tools.terminal_scope import install_and_reset_profile_terminal_scope
    from plugins.symphony_pm.project_terminal import ProjectTerminal
    from plugins.symphony_pm.transport.journal import Journal
    from types import SimpleNamespace

    artifact_dir = Path(os.environ.get('HERMES_E2E_ARTIFACTS', ''))
    spec_file = artifact_dir / 'project-terminal.json'
    if not spec_file.is_file():
        pytest.skip('requires the isolated Windows project terminal qualification')
    settings = json.loads(spec_file.read_bytes())
    home = tmp_path / 'profile'
    home.mkdir()
    root = home / 'plugin-data' / 'symphony_pm' / 'project-terminal'
    project = root / 'project'
    for path in (project / '.tmp', root / 'anchor', root / 'codex', root / 'user' / 'appdata',
                 root / 'user' / 'local', root / 'user' / 'temp'):
        path.mkdir(parents=True)
    from hermes_cli import plugins
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import get_tool_definitions, handle_function_call
    from agent.terminal_env_registry import plugin_backend_names
    shell, client = Path(settings['shell']), Path(settings['client'])
    assert all(p.is_file() for p in (shell, client))
    config = dict(platform_toolsets={'cli': ['terminal']}, terminal={'backend': ProjectTerminal.name, 'cwd': str(project)},
                  plugins={'isolation': 'in_process', 'enabled': ['symphony_pm'], 'entries':
                           {'symphony_pm': {'settings': {'project_terminal': settings}}}})
    (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_RUNTIME_DIR', str(tmp_path / 'runtime'))
    journal = Journal(home, 'symphony_pm:ingress')
    protected = [root / 'codex' / 'canary.txt', journal.path.parent / 'canary.txt', tmp_path / 'sibling' / 'canary.txt']
    for path in protected:
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(b'control outside project\n')
    baseline = [sha(path) for path in protected]
    def launches():
        with sqlite3.connect(journal.path.as_uri() + '?mode=ro', uri=True) as db:
            return db.execute('SELECT count(*) FROM serving_launches').fetchone()[0]
    assert launches() == 0
    with _plugin_home_scope(home), install_and_reset_profile_terminal_scope(home):
        policy = root / 'codex' / 'config.toml'
        provider = ProjectTerminal(SimpleNamespace(get_config=lambda *_: settings))
        policy.write_bytes(provider.config_bytes(shell, Path(settings['python'])))
        plugins._reset_plugin_managers_for_tests()
        try:
            plugins.discover_plugins()
            assert ProjectTerminal.name in plugin_backend_names()
            from tools.terminal_tool import _get_env_config
            assert _get_env_config()['env_type'] == ProjectTerminal.name
            toolsets = sorted(_get_platform_tools(load_config(), 'cli'))
            def catalog(raw):
                return sorted(d['function']['name'] for d in get_tool_definitions(
                    enabled_toolsets=toolsets, quiet_mode=True, skip_tool_search_assembly=raw))
            visible, full = catalog(False), catalog(True)
            assert 'terminal' in full
            assert set(full) <= {'terminal', 'process_manage'}
            manager = json.loads(handle_function_call('tool_call', {'name': 'process_manage', 'arguments': {'action': 'list'}},
                                 task_id='project-terminal', session_id='project-terminal', enabled_toolsets=toolsets))
            assert 'PROJECT_TERMINAL_PROFILE_REQUIRED' in str(manager), manager
            def run(command):
                return json.loads(handle_function_call('terminal', {'command': command, 'workdir': project.as_posix()},
                                  task_id='project-terminal', session_id='project-terminal', enabled_toolsets=toolsets))
            proof = f'"{project / "proof"}"'
            result = run(f'echo project-ok>{proof} && type {proof}')
            assert result.get('exit_code') == 0 and 'project-ok' in result['output'], result
            assert (project / 'proof').read_bytes().strip() == b'project-ok'
            denied_codes = []
            for path in protected:
                quoted = f'"{path}"'
                for command in ('type '+quoted, 'echo changed >> '+quoted):
                    result = run(command)
                    assert result.get('exit_code') in (1, 5) and 'control outside project' not in result['output'], result
                    assert any(s in result['output'].lower() for s in ('access is denied', 'отказано в доступе')), result
                    denied_codes.append(result['exit_code'])
            result = run(f'"{client}" --version')
            assert result.get('exit_code') in (1, 5, 9009) and 'codex-cli' not in result['output'], result
            client_denial = dict(exit_code=result['exit_code'], message=result['output'])
            assert [sha(path) for path in protected] == baseline and launches() == 0
            original = policy.read_bytes()
            policy.write_bytes(b'# unavailable test policy\n')
            result = run('echo unexpected > forbidden')
            assert result.get('exit_code') != 0 and not (project / 'forbidden').exists(), result
            policy.write_bytes(original)
            result = run('type proof')
            assert result.get('exit_code') == 0 and 'project-ok' in result['output'], result
            receipt = dict(config_sha256=sha(home/'config.yaml'), policy_sha256=sha(policy), protected_before=baseline,
                           protected_after=[sha(path) for path in protected], serving_launches=launches(),
                           toolsets=toolsets, visible=visible, full_catalog=full, denial_exit_codes=denied_codes,
                           client_denial=client_denial, deferred_process_manager='BLOCKED')
            (artifact_dir / 'native-result.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        finally:
            plugins._reset_plugin_managers_for_tests()
