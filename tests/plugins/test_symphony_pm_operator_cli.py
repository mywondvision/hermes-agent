"""Read-only diagnostics go through the same argparse registration as the standalone CLI."""
import argparse
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.symphony_pm.status_cli import DEFAULT_NAMESPACE, journal_path, register
from plugins.symphony_pm.transport.schema import initialize


def command(cmd, capsys):
    from hermes_cli.main import _attach_plugin_cli_command
    parser = argparse.ArgumentParser()
    register(SimpleNamespace(register_cli_command=lambda **entry: _attach_plugin_cli_command(
        parser.add_subparsers(), entry)))
    args = parser.parse_args(['symphony', cmd, '--json'])
    assert args.func(args) == 0
    return json.loads(capsys.readouterr().out)


def snapshot(root):
    return (root.exists(), {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None for p in root.rglob('*')})


@pytest.mark.parametrize('existing', [False, True])
def test_cli_leaves_profile_bytes_untouched(tmp_path, monkeypatch, capsys, existing):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    journal = journal_path(tmp_path, DEFAULT_NAMESPACE)
    if existing:
        journal.parent.mkdir(parents=True)
        with sqlite3.connect(journal) as db:
            initialize(db)
    plugin_data = tmp_path/'plugin-data'
    before = snapshot(plugin_data)
    diagnostic = command('doctor', capsys)
    assert diagnostic['journal'] is existing
    assert diagnostic['free_disk_bytes'] > 0 and 'free_ram_bytes' in diagnostic
    assert diagnostic['archive_count'] == 0 and diagnostic['archive_bytes'] == 0
    assert 'QUALIFICATION_UNRECORDED' in diagnostic['reasons']
    assert snapshot(plugin_data) == before
    assert command('status', capsys)['journal'] is existing
    assert snapshot(plugin_data) == before


def test_doctor_reports_journal_admission_holds_without_writing(tmp_path, monkeypatch, capsys):
    from tests.plugins.symphony_serving_support import approve, rig
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    r = rig(tmp_path)
    try:
        request, context, _ = approve(r)
        journal = r.owner.journal
        ticket, _, _ = journal.reserve_launch(r.owner.token, request, context, r.now[0])
        before = snapshot(journal.path.parent)
        assert 'UNSETTLED_PRIOR_LAUNCH_HOLD' in command('doctor', capsys)['reasons']
        assert snapshot(journal.path.parent) == before
        journal.workspace_clear = lambda: True
        assert journal.mark_not_started(r.owner.token, ticket, reason='fixture', preparing=True)
        before = snapshot(journal.path.parent)
        reasons = command('doctor', capsys)['reasons']
        assert 'WORKSPACE_HELD' in reasons and 'UNSETTLED_PRIOR_LAUNCH_HOLD' not in reasons
        assert snapshot(journal.path.parent) == before
    finally:
        r.owner.close(0)


@pytest.mark.platforms('windows')
def test_doctor_rechecks_last_qualification_and_active_slot(tmp_path, monkeypatch, capsys):
    from plugins.symphony_pm.doctor import record_qualification
    from hermes_cli.plugins_loader import _plugin_home_scope
    from tests.plugins.test_symphony_pm_codex_exec import rig
    r = rig(tmp_path)
    try:
        record_qualification(r.owner)
        with _plugin_home_scope(r.home):
            profile_data = r.owner.journal.path.parent
            before = snapshot(profile_data)
            first = command('doctor', capsys)
            assert first['qualification']['checks'] == {'executable': True, 'config': True}
            assert first['active'] is False and snapshot(profile_data) == before
            (r.attempts/'active').mkdir()
            config = Path(r.launcher.qualification.codex_home)/'config.toml'
            config.write_text('fixture change', encoding='utf-8')
            second = command('doctor', capsys)
            assert second['active'] and {'CONFIG_CHANGED_OR_MISSING','ACTIVE_OCCUPIED'} <= set(second['reasons'])
            assert snapshot(profile_data) == before
    finally:
        r.owner.close(0)
