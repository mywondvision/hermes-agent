"""Offline Linux peer for the Desktop acceptance run. Launch as python -m tests.plugins.symphony_plan_peer."""
import asyncio
from contextlib import ExitStack, closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

import pytest

from gateway.run_api import GatewayRuntimeAPI
from gateway.session_events import SessionEvents
from gateway.session_scoped_ws import PROTOCOL
from plugins.symphony_pm.desktop_route import Registration
from tests.plugins.test_symphony_pm_linux_desktop_decision import counts, submit
from tests.plugins.test_symphony_pm_linux_owner_check import make_checker_stand
from tests.plugins.test_symphony_pm_linux_typed_apply import make_linux_chat


class TempDirs:
    def __init__(self, root):
        self.root = root

    def mktemp(self, name):
        return Path(tempfile.mkdtemp(prefix=name, dir=self.root))


class Socket:
    """Only the WebSocket message transport is replaced with in-process ASGI queues."""
    def __init__(self, runner, emit):
        self.incoming = asyncio.Queue()
        self.task = None
        self.accepted = asyncio.Event()
        self.runner, self.emit = runner, emit

    async def open(self, *, app=None, query=b'', path='/api/ws', subprotocols=None, standalone=False):
        async def unexpected(scope, receive, send):
            raise AssertionError('plan socket fell through to dashboard')

        async def send(message):
            if message['type'] == 'websocket.accept':
                if app is None:
                    assert message['subprotocol'] == PROTOCOL
                self.accepted.set()
            elif message['type'] == 'websocket.send':
                self.emit(json.loads(message['text']))
            elif not self.accepted.is_set():
                raise AssertionError('route refused the fixture socket')

        scope = dict(type='websocket', path=path, scheme='ws', headers=[(b'host', b'127.0.0.1')],
                     query_string=query, client=('127.0.0.1', 1), server=('127.0.0.1', 80),
                     subprotocols=[PROTOCOL] if app is None else (subprotocols or []))
        self.scope = scope
        await self.incoming.put({'type': 'websocket.connect'})
        listener = app if standalone else GatewayRuntimeAPI(app or unexpected, self.runner, app)
        self.task = asyncio.create_task(listener(scope, self.incoming.get, send))
        accepted = asyncio.create_task(self.accepted.wait())
        try:
            done, _ = await asyncio.wait((accepted, self.task), timeout=10, return_when=asyncio.FIRST_COMPLETED)
            if self.task in done:
                self.task.result()
            assert accepted in done, 'fixture WebSocket did not open'
        finally:
            accepted.cancel()

    async def frame(self, frame):
        if self.task.done():
            self.task.result()
            raise RuntimeError('socket is closed')
        await self.incoming.put({'type': 'websocket.receive', 'text': json.dumps(frame)})

    async def close(self):
        await self.incoming.put({'type': 'websocket.disconnect', 'code': 1000})
        await asyncio.wait_for(self.task, timeout=10)


class Peer:
    desktop_request_id = 'campaign-gui'

    def __init__(self, session, stand, native, finished, failures, emit):
        self.session = session
        self.native, self.finished, self.failures, self.emit = native, finished, failures, emit
        self.install_id = 'e' * 64
        stand.launcher.qualification.installation_id = self.install_id
        self.sockets = {}
        self.service = ExitStack()
        self.channel = None

    def start(self, public_key, host_fingerprint, restart=False):
        self.channel = c = self.service.enter_context(self.session(
            restart=restart, desktop=True, desktop_request_id=self.desktop_request_id))
        owner = c.owner
        c.runner._draining = False
        c.runner.session_runtime_descriptor = {'state': 'ready'}
        c.runner.session_authority.sessions[c.source.chat_id].event_stream = SessionEvents()
        self.reg = Registration(
            client_id='fixture-desktop', connection_id='fixture-ssh', public_key=public_key,
            install_id=self.install_id, boot_id=owner.guard.identity.boot_id, host_fingerprint=host_fingerprint,
            profile=owner.scope.profile_home, namespace=owner.scope.namespace, operator_id=c.source.user_id,
            conversation_id=c.source.chat_id, registration_id='fixture-registration')
        self.configure(asdict(self.reg))
        return dict(profile=self.reg.profile, namespace=self.reg.namespace, operator_id=self.reg.operator_id,
                    conversation_id=self.reg.conversation_id, session_id=c.entry.session_id, install_id=self.install_id,
                    boot_id=self.reg.boot_id, generation=f'{owner.token.incarnation}:{owner.token.epoch}:{owner.generation}',
                    client_id=self.reg.client_id, connection_id=self.reg.connection_id)

    def configure(self, reg):
        path = Path(self.channel.owner.scope.profile_home) / 'config.yaml'
        config = json.loads(path.read_text(encoding='utf-8'))
        config['plugins']['entries']['symphony_pm']['settings']['desktop_plan_route'] = reg
        path.write_text(json.dumps(config), encoding='utf-8')

    def inspect(self, *, wait_for_completion=True):
        # A fixture observer shares the process with the zero-busy-timeout writer.
        journal = self.channel.owner.journal
        with journal._transaction_lock:
            permits, reservations = counts(self.channel)
        if reservations and wait_for_completion:
            finished = self.finished.wait(timeout=30)
            if self.failures:
                raise AssertionError(f'fixture worker finished={finished}, failures={self.failures!r}') from self.failures[0]
            assert finished, 'fixture worker finished=False, failures=[]'
        audit = journal.path.with_name('typed-plan-audit.sqlite3')
        by_request = {}
        rows = 0
        if audit.exists():
            with journal._transaction_lock, closing(sqlite3.connect(audit.as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('BEGIN')
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='desktop_plan_decisions'").fetchone()
                if exists:
                    rows, = db.execute('SELECT count(*) FROM desktop_plan_decisions').fetchone()
                for (receipt,) in db.execute('SELECT receipt FROM desktop_plan_decisions') if exists else ():
                    row = json.loads(receipt)
                    if row['request_id'] in by_request:
                        raise AssertionError('duplicate decision for campaign request')
                    by_request[row['request_id']] = {k: row[k] for k in ('input_id', 'decision', 'plan_digest')}
        assert rows == len(by_request)
        return dict(permits=permits, reservations=reservations, launches=len(self.native.created),
                    decisions=rows, by_request=by_request)

    async def disconnect(self):
        try:
            await asyncio.gather(*(socket.close() for socket in self.sockets.values()))
        finally:
            self.sockets.clear()
            self.service.close()

    async def control(self, cmd):
        op, key = cmd['op'], cmd.get('socket')
        if op == 'init' and self.channel is None:
            return self.start(cmd['public_key'], cmd['host_fingerprint'])
        if op == 'open' and key not in self.sockets:
            socket = Socket(self.channel.runner, lambda frame: self.emit(dict(socket=key, frame=frame)))
            await socket.open()
            self.sockets[key] = socket
        elif op == 'close':
            await self.sockets.pop(key).close()
        elif op == 'frame':
            await self.sockets[key].frame(cmd['frame'])
        elif op == 'submit':
            action, admission = cmd['action'], cmd['admission']
            assert submit(self.channel, action, admission) == 'WAITING_FOR_AUTHORITY'
            return dict(request_id='qualification-' + action, admission_id=admission)
        elif op == 'counts':
            return await asyncio.to_thread(self.inspect)
        elif op == 'privacy':
            from tests.plugins.test_symphony_pm_linux_route_viewers import privacy
            with pytest.MonkeyPatch.context() as patch:
                return await privacy(self, cmd['request_id'], patch)
        elif op == 'restart':
            await self.disconnect()
            return self.start(self.reg.public_key, self.reg.host_fingerprint, restart=True)
        elif op == 'legacy':
            audit = self.channel.owner.journal.path.with_name('typed-plan-audit.sqlite3')
            with closing(sqlite3.connect(audit)) as db, db:
                updated = db.execute("UPDATE desktop_plan_decisions SET receipt=json_remove(receipt,'$.shown_plan') "
                                     "WHERE json_extract(receipt,'$.request_id')=?", (cmd['request_id'],))
                assert updated.rowcount == 1
        else:
            raise ValueError('unknown fixture operation')
        return True


async def run(peer_class=Peer):
    wire, sys.stdout = sys.stdout, sys.stderr
    def emit(message):
        wire.write(json.dumps(message, ensure_ascii=True) + '\n')
        wire.flush()

    shutdown = None
    with tempfile.TemporaryDirectory(prefix='sr-') as tmp, pytest.MonkeyPatch.context() as patch:
        for name, value in [('HERMES_HOME', 'profile'), ('HERMES_RUNTIME_DIR', 'runtime'), ('HOME', 'home'),
                            ('USERPROFILE', 'home')]:
            path = Path(tmp) / value
            path.mkdir(exist_ok=True)
            patch.setenv(name, str(path))
        patch.setenv('UV_OFFLINE', '1')
        dirs = TempDirs(tmp)
        stand, _, native, finished, failures, session, _ = make_linux_chat(dirs, patch, make_checker_stand(dirs, patch))
        peer = peer_class(session, stand, native, finished, failures, emit)
        try:
            while raw := await asyncio.to_thread(sys.stdin.buffer.readline):
                command = json.loads(raw)
                if command['op'] == 'shutdown':
                    shutdown = command
                    break
                result = await peer.control(command)
                emit(dict(id=command['id'], result=result))
        finally:
            await peer.disconnect()
    if shutdown is not None:
        emit(dict(id=shutdown['id'], result=True))


if __name__ == '__main__':
    asyncio.run(run())
