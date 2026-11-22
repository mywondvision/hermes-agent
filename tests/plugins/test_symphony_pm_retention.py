"""Archive approval always names one immutable, profile-owned candidate set."""
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from plugins.symphony_pm import retention
from plugins.symphony_pm.status_cli import journal_path, read_launches
from plugins.symphony_pm.transport.schema import initialize
from plugins.symphony_pm.transport.types import Refused, digest


def make_owner(home):
    namespace = 'symphony_pm:ingress'
    path = journal_path(home, namespace)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        initialize(db)
    root = home/'work'
    root.mkdir()
    clock = [time.time()]
    callbacks = []
    return SimpleNamespace(journal=SimpleNamespace(path=path), lock=threading.RLock(),
                           scope=SimpleNamespace(profile_home=str(home), namespace=namespace, active=True,
                                                 on_close=callbacks.append),
                           launcher=SimpleNamespace(settings=SimpleNamespace(attempts_root=root)),
                           clock=lambda: clock[0], token=object(), _preparing=set(), _check=lambda *_: None,
                           ticks=clock, callbacks=callbacks)


def archive(owner, number, state='STOPPED', patch=False):
    attempt = f'attempt-{number:032x}'
    workspace = owner.launcher.settings.attempts_root/attempt
    workspace.mkdir()
    private = owner.journal.path.parent/'attempts'/digest(attempt)[:12]
    private.mkdir(parents=True)
    info = workspace.stat()
    identity = [info.st_dev, info.st_ino, getattr(info, 'st_birthtime_ns', None)]
    (private/'workspace-id.json').write_text(json.dumps(identity))
    (workspace/'file').write_bytes(b'abc')
    result = {}
    if patch:
        target = private/'results'/'captured.patch'
        target.parent.mkdir()
        target.write_bytes(b'saved patch')
        result['patch'] = {'path': str(target), 'paths': ['file']}
    with sqlite3.connect(owner.journal.path) as db:
        db.execute('INSERT INTO serving_launches(namespace,project,job,ticket,attempt,state,result,'
                   'stop_proof,proof_source,owner_identity) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (owner.scope.namespace, 'project', str(number), '{"revision":"r"}', attempt, state,
                    json.dumps(result), 'JOB_EMPTY', 'observed', 'a'*64))
    return workspace, private


def test_approval_rechecks_pair_contents_identity_state_and_age(tmp_path):
    owner = make_owner(tmp_path/'one')
    root = owner.launcher.settings.attempts_root
    old = archive(owner, 1)
    changed = archive(owner, 2)
    held = archive(owner, 3)
    young = archive(owner, 4, patch=True)
    swapped = archive(owner, 5)
    active = archive(owner, 6, state='BOUND')
    active[0].rename(root/'active')
    forged = archive(owner, 7)
    (forged[1]/'workspace-id.json').write_text('[]')
    proposed = retention.prune(owner, {})
    assert {a['attempt'] for a in proposed['archives']} == {a[0].name for a in (old, changed, held, swapped)}
    inventory = retention.archive_inventory(owner.journal.path.parent, root, read_launches(owner.journal.path))
    assert next(i['bytes'] for i in inventory if i['attempt'] == old[0].name) == (
        (old[1]/'workspace-id.json').stat().st_size + 3)
    late = archive(owner, 8)
    (changed[0]/'file').write_bytes(b'xyz')
    swapped[1].rename(swapped[1].with_suffix('.saved'))
    swapped[1].mkdir()
    (swapped[1]/'workspace-id.json').write_bytes((swapped[1].with_suffix('.saved')/'workspace-id.json').read_bytes())
    with sqlite3.connect(owner.journal.path) as db:
        db.execute("UPDATE serving_launches SET state='HELD_UNKNOWN' WHERE job='3'")
    other = make_owner(tmp_path/'two')
    archive(other, 1)
    with pytest.raises(Refused):
        retention.prune(other, {}, proposed['code'])
    outcome = retention.prune(owner, {}, proposed['code'])
    assert outcome['removed'] == [old[0].name]
    assert not any(p.exists() for p in old)
    assert set(outcome['skipped']) == {a[0].name for a in (changed, held, swapped)}
    assert all(p.exists() for pair in (changed, held, young, swapped, forged, late) for p in pair)
    assert (root/'active'/'file').is_file()
    assert retention.prune(owner, {}, proposed['code']) == outcome


def test_expired_proposal_and_configured_retention(tmp_path):
    owner = make_owner(tmp_path/'one')
    pair = archive(owner, 1, patch=True)
    archive(owner, 2)
    config = {'patch_retention_days': 10}
    proposal = retention.prune(owner, config)
    owner.ticks[0] += 11 * 86400
    with pytest.raises(Refused):
        retention.prune(owner, config, proposal['code'])
    fresh = retention.prune(owner, config)
    assert pair[0].name in {a['attempt'] for a in fresh['archives']}
    assert all(p.exists() for p in pair)


def test_changed_during_claim_restores_both_trees_before_first_unlink(tmp_path, monkeypatch):
    owner = make_owner(tmp_path/'one')
    workspace, private = archive(owner, 1)
    original = (private/'workspace-id.json').read_bytes()
    proposal = retention.prune(owner, {})
    rename = Path.rename

    def concurrent_edit(source, destination):
        result = rename(source, destination)
        if source == workspace:
            (result/'new').write_bytes(b'late arrival')
        return result

    monkeypatch.setattr(Path, 'rename', concurrent_edit)
    outcome = retention.prune(owner, {}, proposal['code'])
    assert outcome['skipped'] == [workspace.name]
    assert (workspace/'file').read_bytes() == b'abc'
    assert (workspace/'new').read_bytes() == b'late arrival'
    assert (private/'workspace-id.json').read_bytes() == original


@pytest.mark.platforms('windows')
def test_fenced_journal_and_fixture_client_archive_pair(tmp_path):
    from plugins.symphony_pm.transport.recapture import attempt_data
    from tests.plugins.test_symphony_pm_codex_exec import rig, submit
    rigged = rig(tmp_path)
    try:
        ticket, _ = submit(rigged)
        owner = rigged.owner
        inventory = retention.archive_inventory(owner.journal.path.parent, rigged.attempts,
                                                read_launches(owner.journal.path))
        assert len(inventory) == 1 and inventory[0]['attempt'] == ticket.attempt and inventory[0]['bytes'] > 0
        assert not retention.prune(owner, {})['archives']
        rigged.now[0] = time.time() + 31 * 86400
        proposal = retention.prune(owner, {})
        assert retention.prune(owner, {}, proposal['code'])['removed'] == [ticket.attempt]
        assert not (rigged.attempts/ticket.attempt).exists()
        assert not attempt_data(rigged.launcher.qualification, ticket).exists()
    finally:
        rigged.owner.close(0)


def test_weekly_notice_scoped_and_only_once_across_restart(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    owner = make_owner(tmp_path/'one')
    pair = archive(owner, 1)
    seen = []
    delivered = threading.Event()
    scanning = threading.Event()
    resume = threading.Event()
    scan = retention._scan

    def slow_archive(*args, **kwargs):
        scanning.set()
        assert resume.wait(10)
        return scan(*args, **kwargs)

    monkeypatch.setattr(retention, '_scan', slow_archive)

    def inject(content, *, session_key):
        seen.append((str(get_hermes_home()), session_key, content))
        delivered.set()
        return True

    config = dict(session_key='operator-chat', platform='telegram', user_id='42', chat_id='42')
    ctx = SimpleNamespace(inject_message=inject, get_config=lambda *_: config, _gateway_injection_allowed=lambda: True)
    retention.start_weekly(ctx, owner, config)
    try:
        assert scanning.wait(5)
        assert owner.lock.acquire(timeout=3), 'a weekly archive scan must allow STOP'
        owner.lock.release()
        resume.set()
        assert delivered.wait(5)
    finally:
        resume.set()
        owner.callbacks.pop()(time.monotonic()+5)
    home, key, content = seen[0]
    assert Path(home) == Path(owner.scope.profile_home) and key == 'operator-chat'
    assert pair[0].name in content and '/symphony prune approve ' in content
    assert all(p.exists() for p in pair)
    retention._weekly_tick(ctx, owner)
    assert len(seen) == 1
    owner.ticks[0] += 7 * 86400 + 1
    retention._weekly_tick(ctx, owner)
    assert len(seen) == 2


@pytest.mark.parametrize('allow,host_raises', [(False, False), (True, False), (True, True)])
def test_weekly_gateway_admission_and_uncertain_ack(tmp_path, allow, host_raises):
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from hermes_cli.plugins_loader import _plugin_home_scope
    from plugins.symphony_pm.operator_commands import attach_operator

    home = tmp_path/'operator-home'
    owner = make_owner(home)
    workspace, private = archive(owner, 1)
    note = workspace/'note.txt'
    note.write_text('private workspace content', encoding='utf-8')
    size = (note.stat().st_size + (workspace/'file').stat().st_size + (private/'workspace-id.json').stat().st_size)
    key = 'agent:main:telegram:dm:42'
    config = dict(platform='telegram', user_id='42', chat_id='42', session_key=key)
    entry = {'settings': {'operator': config}}
    if allow:
        entry['allow_gateway_injection'] = True
    (home/'config.yaml').write_text(json.dumps({'plugins': {'entries': {'symphony_pm': entry}}}), encoding='utf-8')
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name='symphony_pm', key='symphony_pm', source='bundled'), manager)
    seen = []

    def gateway(**message):
        seen.append(message)
        if host_raises:
            raise OSError('acknowledgment unavailable after scheduling')
        return True

    manager.set_gateway_message_injector(object(), gateway)
    ingress = SimpleNamespace(scope=owner.scope, binding=SimpleNamespace(port=SimpleNamespace(owner=owner)))
    if not allow:
        assert retention._weekly_tick(ctx, owner) == 3600
        assert seen == [] and not owner.journal.path.with_name('prune-weekly.json').exists()
        return
    with _plugin_home_scope(home):
        attach_operator(ctx, ingress)
    try:
        from tests.waiting import wait_until
        marker = owner.journal.path.with_name('prune-weekly.json')
        assert wait_until(marker.is_file, timeout=5)
    finally:
        owner.callbacks.pop()(time.monotonic()+5)
    code = retention.prune(owner, config)['code']
    expected = (f'Symphony archives: 1 ({size} bytes).\n{workspace.name}  {size} bytes\n'
                f'/symphony prune approve {code}')
    assert seen == [dict(plugin_id='symphony_pm', plugin_home=home, session_key=key, content=expected)]
    owner.ticks[0] += 3601
    retention._weekly_tick(ctx, owner)
    assert len(seen) == 1
    assert note.is_file() and private.is_dir()


@pytest.mark.parametrize('replacement', [{}, dict(platform='telegram', session_key='new', chat_id='20', user_id='20')])
def test_weekly_rechecks_operator_address_after_scan(tmp_path, monkeypatch, replacement):
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    home = tmp_path/'home'
    owner = make_owner(home)
    archive(owner, 1)
    cfg = home/'config.yaml'

    def configure(address):
        cfg.write_text(json.dumps({'plugins': {'entries': {'symphony_pm': {
            'allow_gateway_injection': True, 'settings': {'operator': address}}}}}), encoding='utf-8')

    configure(dict(platform='telegram', session_key='old', chat_id='10', user_id='10'))
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name='symphony_pm', key='symphony_pm', source='bundled'), manager)
    seen = []
    def accept(**message):
        seen.append(message)
        return True
    manager.set_gateway_message_injector(object(), accept)
    scan = retention._scan
    def replace_address(root, **options):
        result = scan(root, **options)
        configure(replacement)
        return result
    with monkeypatch.context() as m:
        m.setattr(retention, '_scan', replace_address)
        retention._weekly_tick(ctx, owner)
    assert seen == [] and not owner.journal.path.with_name('prune-weekly.json').exists()
    retention._weekly_tick(ctx, owner)
    assert [msg['session_key'] for msg in seen] == (['new'] if replacement else [])


def test_closing_owner_during_final_scan_restores_pair(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    owner = make_owner(tmp_path/'one')
    workspace, private = archive(owner, 1)
    proposal = retention.prune(owner, {})
    at_claim = threading.Event()
    revoked = threading.Event()

    def check(token):
        if token != owner.token or not owner.scope.active:
            raise Refused('WRITER_FENCED')

    owner._check = check
    scan = retention._scan
    def final_scan(path, **options):
        result = scan(path, **options)
        if path.parent.name.startswith('.p-'):
            at_claim.set()
            assert revoked.wait(5)
        return result

    monkeypatch.setattr(retention, '_scan', final_scan)
    with ThreadPoolExecutor(max_workers=1) as worker:
        request = worker.submit(retention.prune, owner, {}, proposal['code'])
        try:
            assert at_claim.wait(5)
            owner.scope.active = False
        finally:
            revoked.set()
        outcome = request.result(timeout=5)
    assert outcome['skipped'] == [workspace.name] and outcome['incomplete'] == []
    assert (workspace/'file').read_bytes() == b'abc' and (private/'workspace-id.json').is_file()


def test_closing_owner_after_first_unlink_protects_remainder(tmp_path, monkeypatch):
    owner = make_owner(tmp_path/'one')
    workspace, private = archive(owner, 1)
    (workspace/'another').write_bytes(b'keep')
    proposal = retention.prune(owner, {})
    unlink = Path.unlink
    removed = []

    def stop_after_unlink(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path.parent.name == 'a' and path.parent.parent.name.startswith('.p-'):
            removed.append(path.name)
            owner.scope.active = False
        return result

    def check(token):
        if not owner.scope.active:
            raise Refused('WRITER_FENCED')

    owner._check = check
    monkeypatch.setattr(Path, 'unlink', stop_after_unlink)
    result = retention.prune(owner, {}, proposal['code'])
    assert result['incomplete'] == [workspace.name] and result['removed'] == []
    assert len(removed) == 1 and {p.name for p in workspace.iterdir()} == {'another', 'file'} - set(removed)
    assert (private/'workspace-id.json').is_file()
