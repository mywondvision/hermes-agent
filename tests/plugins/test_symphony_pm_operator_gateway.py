"""Human-only commands enter through the ordinary offline gateway dispatcher."""
from dataclasses import replace
import importlib
from itertools import count
import json
import time
from types import SimpleNamespace

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from hermes_cli import plugins
from hermes_cli.plugins_loader import _plugin_home_scope
from plugins.symphony_pm.operator_commands import register_operator
from plugins.symphony_pm.transport.port import ServingPort
from tests.gateway.test_plugin_command_choice import _runner, _source, _TextAdapter
from tests.plugins.test_symphony_pm_control_status import call, control, reserved
from tests.plugins.test_symphony_pm_operator_cli import command
from tests.plugins.test_symphony_pm_serving import loaded as discovered_host


def surface(owner, monkeypatch):
    runner = _runner(_TextAdapter())
    source = _source()
    runner.session_store.lookup_by_session_key.return_value = None
    key = runner._session_key_for_source(source)
    config = dict(user_id=source.user_id, chat_id=source.chat_id, platform=source.platform.value, session_key=key)
    hooks = {}
    ctx = SimpleNamespace(get_config=lambda key, default: config, register_command=lambda name, fn, **opts: fn,
                          register_hook=hooks.__setitem__)
    ingress = SimpleNamespace(scope=owner.scope, binding=SimpleNamespace(port=ServingPort(owner)))
    handler = register_operator(ctx, SimpleNamespace(get=lambda: ingress))
    from hermes_cli import lifecycle
    invoke = lifecycle.ainvoke_hook
    async def invoke_with_operator(name, **kwargs):
        if name == 'pre_gateway_dispatch':
            hooks[name](kwargs['event'], kwargs['gateway'])
            return []
        return await invoke(name, **kwargs)
    monkeypatch.setattr(lifecycle, 'ainvoke_hook', invoke_with_operator)
    monkeypatch.setattr(plugins, 'get_plugin_command_handler', lambda name: handler if name == 'symphony' else None)
    return runner, source, handler


_MESSAGE_IDS = count(1)


async def send(runner, source, args, *, message_type=MessageType.TEXT, original=True):
    text = '/symphony ' + args
    message_id = next(_MESSAGE_IDS)
    raw = SimpleNamespace(text=text, message_id=message_id, from_user=SimpleNamespace(id=source.user_id, is_bot=False),
                          chat=SimpleNamespace(id=source.chat_id, type='private'), edit_date=None, forward_origin=None,
                          via_bot=None, sender_chat=None, voice=None, audio=None, business_connection_id=None,
                          sender_business_bot=None)
    return await runner._handle_message(MessageEvent(text=text, source=source, message_type=message_type,
                                        message_id=str(message_id), raw_message=raw if original else None))


@pytest.mark.asyncio
@pytest.mark.parametrize('profile_import', [True, False])
async def test_discovered_plugin_routes_to_serving_owner(discovered_host, profile_import):
    from hermes_cli.approval_transport import RegisteredApprovalTransport
    from tests.plugins.symphony_serving_support import Guard, Native, Observer, approve
    from tests.plugins.test_symphony_pm_adapter import job_payload
    from tests.plugins.test_symphony_pm_serving import _call
    home, host, module = discovered_host
    package = module.__package__ if profile_import else 'plugins.symphony_pm'
    transport = importlib.import_module(package + '.transport.bootstrap')
    lifecycle = importlib.import_module(package + '.transport.lifecycle')
    now = [time.time()]
    class FixtureObserver(Observer):
        def handshake(self, identity, generation, targets):
            original = super().handshake(identity, generation, targets)
            return lifecycle.ObserverHandshake(**vars(original))
    channel = RegisteredApprovalTransport('fixture', lambda *_: pytest.fail('unexpected prompt'), 'test', str(home))
    bootstrap = transport.TransportBootstrap(Guard, lambda _: FixtureObserver(now), lambda _: Native(),
        lambda *args: pytest.fail('should stop before launch'), channel, 'serving-test', 'controller', ('issuer',))
    bindings = []
    source = _source()
    runner = _runner(_TextAdapter())
    runner.session_store.lookup_by_session_key.return_value = None
    config = {'platform': source.platform.value, 'user_id': source.user_id, 'chat_id': source.chat_id,
              'session_key': runner._session_key_for_source(source)}
    def factory(scope):
        binding = bootstrap(scope)
        bindings.append(binding)
        return binding
    host.configure(home, {('symphony_pm', 'ingress'): module.ServingBootstrap(factory)})
    host.start_profile(home)
    # Enable the sender after startup: the host reads the profile's current operator policy.
    (home/'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['symphony_pm'], 'entries': {
        'symphony_pm': {'settings': {'operator': config}}}}}), encoding='utf-8')
    with _plugin_home_scope(home):
        assert json.loads(await send(runner, source, 'status')) == {'jobs': []}
        binding, = bindings
        owner = binding.port.owner
        req, auth, _ = approve(SimpleNamespace(owner=owner, scope=owner.scope, now=now))
        ticket, _, _ = owner.journal.reserve_launch(owner.token, req, auth, now[0])
        payload = job_payload('get_status', job_ref=ticket.job, expected_job_revision=ticket.revision)
        assert _call(payload, session_id='session-1')['view']['phase'] == 'PENDING'
        for command in ('release '+ticket.job+' fixture', 'prune', 'prune approve 1234'):
            assert await send(runner, source, command, message_type=MessageType.VOICE) == 'ORIGINAL_TYPED_COMMAND_REQUIRED'
            assert await send(runner, source, command, original=False) == 'ORIGINAL_TYPED_COMMAND_REQUIRED'
        assert await send(runner, source, 'release '+ticket.job+' fixture') == 'RELEASE_REQUIRES_OWNER_REASON'
        assert await send(runner, source, 'recapture '+ticket.job) == 'RECAPTURE_REQUIRES_PROVEN_STOP'
        assert json.loads(await send(runner, source, 'stop '+ticket.job))['state'] == 'CANCEL_REQUESTED'


@pytest.mark.asyncio
async def test_gateway_sender_and_idle_stop(reserved, monkeypatch, tmp_path):
    r = reserved
    runner, source, _ = surface(r.owner, monkeypatch)
    with _plugin_home_scope(tmp_path):
        for foreign in (replace(source, user_id='other'), replace(source, is_bot=True), replace(source, chat_id='other')):
            assert await send(runner, foreign, 'stop '+r.ticket.job) == 'OWNER_SENDER_REQUIRED'
        assert await send(runner, source, 'release '+r.ticket.job+' fixture') == 'RELEASE_REQUIRES_OWNER_REASON'
        assert await send(runner, source, 'recapture '+r.ticket.job) == 'RECAPTURE_REQUIRES_PROVEN_STOP'
        r.owner.execute_reserved(r.owner.token, r.ticket, r.submit)
        outcome = json.loads(await send(runner, source, 'stop '+r.ticket.job))
        assert outcome['dispatched'] and r.native.created[0].cancels == 1
        assert outcome['hold']


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['PENDING', 'RUNNING', 'STOPPED', 'HELD_UNKNOWN', 'NOT_STARTED'])
async def test_three_surfaces_for_one_observation(reserved, monkeypatch, tmp_path, capsys, phase):
    r = reserved
    if phase == 'RUNNING':
        r.owner.execute_reserved(r.owner.token, r.ticket, r.submit)
    elif phase == 'STOPPED':
        def finish_without_result(ticket, request, run):
            run(['fixture', 'exec'], cwd=ticket.plan.workspace, timeout=1)
            r.observer.stopped = True
            assert r.owner.finish_client_job(r.owner.token, ticket)[0]
        finish_without_result.job_coverage_for = lambda guard: True
        r.owner.launcher = finish_without_result
        r.owner.execute_reserved(r.owner.token, r.ticket, r.submit)
    elif phase == 'HELD_UNKNOWN':
        r.owner.journal.claim_create(r.owner.token, r.ticket)
    elif phase == 'NOT_STARTED':
        r.owner.journal.mark_not_started(r.owner.token, r.ticket)
    runner, source, _ = surface(r.owner, monkeypatch)
    with _plugin_home_scope(tmp_path):
        model = call(r, *control(r))['view']
        assert model['phase'] == phase
        slash = json.loads(await send(runner, source, 'status '+r.ticket.job+' --saved'))
        cli = command('status', capsys)['launches'][0]
        assert cli['view'] == slash['view'] == model
        assert json.loads(await send(runner, source, 'status '+r.ticket.job))['view']['phase'] == phase


@pytest.mark.asyncio
async def test_busy_stop_is_queued_without_lifecycle_effect(reserved, monkeypatch, tmp_path):
    from gateway.run import _AGENT_PENDING_SENTINEL
    runner, source, _ = surface(reserved.owner, monkeypatch)
    key = runner._session_key_for_source(source)
    runner._session_state(key).turn.agent = _AGENT_PENDING_SENTINEL
    with _plugin_home_scope(tmp_path):
        output = await send(runner, source, 'stop '+reserved.ticket.job)
        assert output is None and runner._is_session_running(key)
        queued = runner.adapters[source.platform]._pending_messages[key]
        assert queued.text == '/symphony stop ' + reserved.ticket.job
        assert reserved.owner.journal.read_launch(reserved.owner.token, reserved.ticket.project,
                                                  reserved.ticket.job, reserved.ticket.revision)[1]['state'] == 'LAUNCH_INTENT'


@pytest.mark.asyncio
async def test_owner_release_recovered_hold(tmp_path, monkeypatch):
    from tests.plugins.test_symphony_pm_create_recovery import _predecessor, _successor
    from tests.plugins.symphony_serving_support import Native
    monkeypatch.setattr(Native, 'ownership_contract', None)
    ticket = _predecessor(tmp_path, monkeypatch, create_entered=True)
    successor = _successor(tmp_path)
    try:
        runner, source, _ = surface(successor.owner, monkeypatch)
        with _plugin_home_scope(tmp_path):
            rejected = await send(runner, source, 'release '+ticket.job+' owner reviewed fixture', original=False)
            assert rejected == 'ORIGINAL_TYPED_COMMAND_REQUIRED'
            result = await send(runner, source, 'release '+ticket.job+' owner reviewed fixture')
            assert json.loads(result)['state'] == 'RELEASED_BY_OWNER'
            assert await send(runner, source, 'recapture '+ticket.job) == 'RECAPTURE_REQUIRES_PROVEN_STOP'
    finally:
        successor.owner.close(0)


def test_local_tui_and_cli_fallback_have_no_sender(reserved, monkeypatch, capsys):
    from cli import HermesCLI
    from hermes_cli import gateway_plugin_commands
    from tui_gateway.server import _run_plugin_command
    _, _, handler = surface(reserved.owner, monkeypatch)
    args = 'stop '+reserved.ticket.job
    assert _run_plugin_command(handler, args) == 'OWNER_SENDER_REQUIRED'
    monkeypatch.setattr(gateway_plugin_commands, 'run', lambda *args: None)
    cli = SimpleNamespace(session_id='sess-1', _clarify_callback=lambda *args: None)
    HermesCLI._run_plugin_slash_command(cli, '/symphony', args)
    reply = capsys.readouterr()
    assert 'OWNER_SENDER_REQUIRED' in reply.out + reply.err


@pytest.mark.asyncio
@pytest.mark.platforms('windows')
async def test_gateway_recapture_after_proven_fixture_stop(tmp_path, monkeypatch, capsys):
    from plugins.symphony_pm.doctor import record_qualification
    from tests.plugins.test_symphony_pm_codex_exec import rig, submit
    r = rig(tmp_path)
    try:
        ticket, _ = submit(r)
        runner, source, _ = surface(r.owner, monkeypatch)
        with _plugin_home_scope(r.home):
            result = await send(runner, source, 'recapture '+ticket.job)
            output = json.loads(result)
            row = r.owner.journal.read_launch(r.owner.token, ticket.project, ticket.job, ticket.revision)[1]
            assert output['patch_sha256'] == json.loads(row['result'])['patch']['sha256'] and output['seq'] > 0
            record_qualification(r.owner)
            inventory = command('doctor', capsys)
            assert inventory['archive_count'] == 1 and inventory['archive_bytes'] > 0
            proposal = json.loads(await send(runner, source, 'prune'))
            assert proposal['archives'] == []  # the recaptured patch is new and unapplied
    finally:
        r.owner.close(0)
