"""Fresh offline owner of an existing synthetic OPERATOR database."""

import asyncio
import base64
import faulthandler
import importlib.util
import ipaddress
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from threading import RLock
import time
from types import SimpleNamespace
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _encoded(data):
    return base64.b64encode(data).decode('ascii')


def _offline_network(event, args):
    if event in ('socket.connect', 'socket.bind', 'socket.sendto') and isinstance(args[1], tuple):
        host = args[1][0]
    elif event in ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr'):
        host = args[0]
    else:
        return
    if host is None or host == 'localhost':
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise PermissionError('offline OPERATOR peer attempted a non-loopback network operation')


async def first_owner(args):
    import pytest
    from tests.plugins.symphony_plan_peer import TempDirs
    from tests.plugins.test_symphony_pm_linux_operator import operator_roundtrip
    from tests.plugins.test_symphony_pm_linux_owner_check import make_checker_stand
    from tests.plugins.test_symphony_pm_linux_typed_apply import make_linux_chat

    root = Path(args['scratch']).resolve(strict=True)
    with pytest.MonkeyPatch.context() as patch:
        for name in ('HERMES_HOME', 'HERMES_RUNTIME_DIR', 'HOME', 'USERPROFILE'):
            target = root / name.lower()
            target.mkdir(exist_ok=True)
            patch.setenv(name, str(target))
        dirs = TempDirs(root)
        fixture = make_linux_chat(dirs, patch, make_checker_stand(dirs, patch))
        followup = args.get('followup')
        return await operator_roundtrip(fixture, patch, root, revoke_before_cancel=followup is None, followup=followup)


async def restarted_owner(args):
    from gateway.control_socket import GatewayControlServer
    from gateway.runtime_ownership import process_ownership
    from gateway.session_controls import AuthorityConnection
    from gateway.session_desktop_operator import payload_digest
    from gateway.session_forward import install_forward_verbs
    from gateway.session_scoped_ws import register
    from hermes_cli.plugin_request_binding import canonical_home
    from plugins.symphony_pm.desktop_route import PlanRoute
    from plugins.symphony_pm.transport.linux_lifecycle import LinuxLifecycleOwner
    from tests.gateway.test_session_authorities_multiplex import _runner
    from tests.plugins.symphony_task_peer import initialize_task_gateway
    from tests.plugins.test_symphony_pm_linux_lifecycle import Guard, Launcher, Native, Observer
    from tui_gateway.prompt_forward import ForwardClient

    home = Path(args['home']).resolve(strict=True)
    assert Path(args['profile_id']).resolve(strict=True) == home
    os.environ['HERMES_HOME'] = str(home)
    config_path = home / 'config.yaml'
    settings = json.loads(config_path.read_text(encoding='utf-8'))['plugins']['entries']['symphony_pm']['settings']
    cleanup = []
    scope = SimpleNamespace(active=True, profile_home=canonical_home(home), namespace=args['namespace'],
                            on_close=cleanup.append)

    class QueryOnlyLauncher(Launcher):
        def __call__(self, *args):
            raise AssertionError('recovery started a workload')

    class QueryOnlyNative(Native):
        def create_suspended(self, *args):
            raise AssertionError('recovery created a workload')

    native, observer, launcher = QueryOnlyNative(), Observer([time.time()]), QueryOnlyLauncher()
    observer.stopped = True
    launcher.qualification = SimpleNamespace(installation_id=settings['desktop_plan_route']['install_id'])
    guard = Guard(scope, 'operator-restart-' + uuid4().hex, predecessor_dead=True)
    process_ownership.reserve([home])
    runner, owner, server, viewer = None, None, None, None
    try:
        runner = _runner(home, [('default', home)])
        await initialize_task_gateway(runner)
        runner._running, runner._gateway_loop, runner._draining = True, asyncio.get_running_loop(), False
        runner.session_runtime_descriptor['state'] = 'ready'
        authority = runner.session_authority
        assert Path(authority.db.db_path).resolve() == Path(args['state_db']).resolve(strict=True)
        assert authority.epoch == args['gateway_epoch'] + 1 and authority.instance_id != args['gateway_instance']
        viewer = AuthorityConnection(authority, object(), {'user_id': 'fixture', 'provider': 'session-token'})
        attached = await viewer.dispatch(dict(id=1, method='session.resume', params=dict(
            session_id=args['session_id'], omit_messages=True)))
        assert 'result' in attached and args['session_id'] in authority.sessions, attached.get('error')

        owner = LinuxLifecycleOwner(scope, guard, observer, native, launcher,
                                    scheduler_id='serving-test', controller='fixture')
        assert owner.token.epoch == args['symphony_epoch'] + 1
        approval = SimpleNamespace(scope=scope, owner=owner, ctx=SimpleNamespace(get_config=settings.get),
                                   writer=owner.journal.restrict_permits(lambda _identity: None))
        register(PlanRoute(approval), scope)
        server = GatewayControlServer(home)
        install_forward_verbs(server, runner, asyncio.get_running_loop())
        assert await server.start()

        ref = dict(profile_id=authority.profile_id, session_id=args['session_id'])
        target = home, authority.instance_id, ref
        transport = uuid4().hex
        installation, window = (Ed25519PrivateKey.from_private_bytes(base64.b64decode(args[key]))
                                for key in ('installation_seed', 'window_seed'))
        install_public, window_public = (_encoded(key.public_key().public_bytes_raw())
                                         for key in (installation, window))

        async def action(verb, generation, payload):
            issued = await asyncio.to_thread(
                ForwardClient._call, target, 'operator-challenge', transport_id=transport,
                action=verb, submission_id=args['submission_id'], installation_key=install_public,
                window_key=window_public, window_generation=generation, payload_sha256=payload_digest(payload, action=verb),
            )
            assert 'operator_error' not in issued, issued
            message = base64.b64decode(issued['message'])
            return await asyncio.to_thread(
                ForwardClient._call, target, 'operator-execute', transport_id=transport,
                challenge_id=issued['challenge_id'], payload=payload,
                installation_signature=_encoded(installation.sign(message)), window_signature=_encoded(window.sign(message)),
            )

        command = args['command']
        prior = await action('stop_result', args['window_generation'], command)
        assert prior == {'operator_error': 'OPERATOR_HOLD'}, prior
        moved = await action('recover', args['window_generation'], {})
        generation = args['window_generation'] + 1
        assert moved['window_generation'] == generation, moved
        result = await action('stop_result', generation, command)
        assert result == args['receipt'], result
        sealed = args.get('sealed')
        extra = {}
        if sealed is not None:
            original = {key: sealed[key] for key in ('attempt_id', 'command_id')}
            assert await action('stop_result', generation, original) == sealed
            assert await action('stop', generation, dict(original, reason=args['old_reason'])) == sealed
            unproven = await action('stop_reconcile', generation,
                                    dict(original, command_id='missing-after-restart', reason=args['old_reason']))
            assert unproven['state'] == 'UNKNOWN'
            extra.update(old_stop=sealed, no_witness=unproven['state'])
            payload = dict(command, previous_command_id=sealed['command_id'], reason=args['reason'])
            duplicate = await action('stop_replace', generation, payload)
        else:
            duplicate = await action('stop', generation, dict(command, reason=args['reason']))
        assert duplicate == result
        assert not native.created and not owner._sent_cancel
        with closing(sqlite3.connect(owner.journal.path.as_uri() + '?mode=ro', uri=True)) as db:
            causes = db.execute('SELECT COUNT(*) FROM serving_cancel_causes').fetchone()[0]
        assert causes == args['cancel_causes'], causes
        return dict(pid=os.getpid(), gateway_epoch=authority.epoch, gateway_instance=authority.instance_id,
                    symphony_epoch=owner.token.epoch, window_generation=generation, stop=result,
                    cancel_causes=causes, repeat_dispatches=len(owner._sent_cancel), workload_creates=len(native.created),
                    **extra)
    finally:
        if server is not None:
            await server.stop()
        for close_route in cleanup:
            close_route(None)
        if owner is not None:
            owner.close(time.monotonic() + 5)
        if viewer is not None:
            await viewer.close()
        if runner is not None and getattr(runner, 'session_authority', None):
            runner.session_authority.db.close()
        process_ownership.release(home)


def _load_source(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def previous_reader(args):
    """Execute the pinned predecessor's reader without serving a socket or opening the live DB."""
    from gateway.session_contract import SessionRef
    from gateway.session import SessionSource
    from gateway.config import Platform
    from hermes_state import SessionDB

    files = Path(args['reader']).resolve(strict=True)
    old = _load_source('gateway.operator_previous_authority', files / 'session_authority.py')
    controls = _load_source('gateway.operator_previous_controls', files / 'session_controls.py')
    ref = SessionRef(args['profile_id'], args['session_id'])
    outcomes = {}
    for name in ('copied_private', 'isolated_profile'):
        with closing(SessionDB(Path(args[name]), read_only=True)) as db:
            authority = old.SessionAuthority(SimpleNamespace(), profile_id=ref.profile_id,
                                             instance_id='previous-reader', db=db, epoch=1)
            source = SessionSource(platform=Platform.LOCAL, user_id=args['host_subject'],
                                   chat_id=ref.session_id, chat_type='dm')
            authority.sessions[ref.session_id] = SimpleNamespace(source=source, event_stream=SimpleNamespace(
                lock=RLock()), settling={})
            connection = controls.AuthorityConnection(authority, object(),
                                                       {'provider': 'session-token', 'user_id': 'synthetic-reader'})
            response = await connection.dispatch(dict(id=1, method='prompt.receipt', params=dict(
                session_id=ref.session_id, admission_id=args['admission_id'], include_result=True)))
            result = response.get('result', {})
            outcomes[name] = dict(admission_visible=result.get('admission_id') == args['admission_id'],
                                  result_visible='result' in result, usage_visible='usage' in result,
                                  reason=response.get('error', {}).get('message'))
    return outcomes


if __name__ == '__main__':
    sys.addaudithook(_offline_network)
    faulthandler.dump_traceback_later(40, repeat=True)
    data = json.load(sys.stdin)
    wire, sys.stdout = sys.stdout, sys.stderr
    selected = {'first_owner': first_owner, 'restart': restarted_owner, 'previous_reader': previous_reader}[data['operation']]
    result = asyncio.run(selected(data))
    wire.write(json.dumps(result) + '\n')
