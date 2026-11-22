"""Offline standalone ASGI and same-user control IPC. The canonical fixture seeds the bound chat."""
import asyncio
import json
from pathlib import Path
from threading import Lock

import pytest

from gateway.control_socket import GatewayControlServer
from gateway.session_forward import install_forward_verbs
from hermes_cli import web_server
from hermes_cli.web_server_idle_exit import IdleClientTracker
from tests.plugins.symphony_plan_peer import Socket, run
from tests.plugins.symphony_task_peer import TaskPeer


class ForwardPeer(TaskPeer):
    control_socket = None

    def _count_handler(self, name, handler):
        def counted(params, principal):
            with self.counter_lock:
                self.ipc[name]['entered'] += 1
            result = handler(params, principal)
            with self.counter_lock:
                self.ipc[name]['returned'] += 1
                if result.get('conflict') is True:
                    self.ipc[name]['conflicts'] += 1
            return result
        return counted

    def _busy(self, cmd):
        action = cmd['action']
        if action == 'arm' and self.busy_state == 'idle':
            self.busy_state = 'armed'
        elif action == 'release' and self.busy_state == 'occupied' and cmd.get('admission_id') == self.busy_id:
            self.busy_state = 'released'
            self.release_turn.set()
        elif action != 'state':
            raise ValueError('invalid barrier transition')
        return dict(state=self.busy_state, admission_id=self.busy_id)

    def _install_observers(self, patch):
        from gateway.session_ingress import viewer_admission_id
        from tui_gateway import server

        self.counter_lock = Lock()
        self.ordinary_turns = 0
        self.ipc = {}
        for name, handler in self.control_socket.private_handlers.copy().items():
            self.ipc[name] = dict(entered=0, returned=0, conflicts=0)
            self.control_socket.private_handlers[name] = self._count_handler(name, handler)

        self.busy_state, self.busy_id = 'idle', None
        self.release_turn = asyncio.Event()
        model = self.runner._run_agent

        async def bound_turn(*args, **kwargs):
            if self.busy_state == 'armed':
                self.busy_id = viewer_admission_id.get()
                assert self.busy_id
                self.busy_state = 'occupied'
                await self.release_turn.wait()
            return await model(*args, **kwargs)

        patch.setattr(self.runner, '_run_agent', bound_turn)

        def record_ordinary(*args):
            with self.counter_lock:
                self.ordinary_turns += 1

        patch.setattr(server, '_start_agent_build', lambda *args, **kwargs: None)
        patch.setattr(server, '_run_after_agent_ready', record_ordinary)

    async def control(self, cmd):
        if cmd['op'] == 'busy':
            return self._busy(cmd)
        if cmd['op'] == 'counts':
            counts = await super().control(cmd)
            with self.counter_lock:
                counts.update(ipc={name: dict(row) for name, row in self.ipc.items()},
                              ordinary_turns=self.ordinary_turns)
            return counts
        if cmd['op'] == 'init':
            if self.channel is not None:
                raise ValueError('forward fixture already initialized')
            token = cmd['ssh_token']
            assert isinstance(token, str) and len(token) >= 24
            manifest = await self.init_task(cmd)
            home = Path(self.authority.profile_id)
            inbox = asyncio.Queue()
            setup = await self.ordinary(inbox.put_nowait)
            try:
                await setup.frame(dict(jsonrpc='2.0', id='ordinary', method='session.create', params=dict(
                    source='gui', request_id='ordinary-chat', model='fixture', toolsets=[])))
                while True:
                    frame = await asyncio.wait_for(inbox.get(), timeout=15)
                    if frame.get('id') == 'ordinary':
                        manifest['ordinary_session_id'] = frame['result']['session_id']
                        break
            finally:
                await setup.close()
            config_path = home / 'config.yaml'
            config = json.loads(config_path.read_text('utf-8'))
            settings = config['plugins']['entries']['symphony_pm']['settings']
            settings['desktop_forward_modes'] = {manifest['conversation_id']: 'shared',
                                                 manifest['ordinary_session_id']: 'ordinary'}
            config_path.write_text(json.dumps(config), 'utf-8')
            descriptor = self.runner.session_runtime_descriptor
            # The fixture has no network listener. The production discovery parser still
            # requires an advertised loopback origin; the private verbs never dial it.
            def identify():
                return dict(descriptor, supervisor='none', api_origin='http://127.0.0.1:1',
                            capabilities=['session-authority-v1'])
            self.control_socket = GatewayControlServer(home, verb_handlers={'identify': identify})
            install_forward_verbs(self.control_socket, self.runner, asyncio.get_running_loop())
            assert await self.control_socket.start()
            patch = self.service.enter_context(pytest.MonkeyPatch.context())
            from tui_gateway import server
            self._install_observers(patch)
            patch.setattr(server, '_hermes_home', home)
            # Every in-process peer is a fresh standalone server with its own launch store.
            patch.setattr(server, '_db', None)
            patch.setattr(server, '_db_error', None)
            patch.setattr(web_server, '_SESSION_TOKEN', token)
            for field, value in dict(session_authority=None, ui_surface='standalone', ssh_forward_home=home,
                                     ssh_isolated_clients=IdleClientTracker()).items():
                patch.setattr(web_server.app.state, field, value, raising=False)
            return manifest
        if cmd['op'] == 'open' and cmd.get('channel') == 'ordinary':
            key = cmd['socket']
            assert key not in self.sockets
            socket = Socket(None, lambda frame: self.emit(dict(socket=key, frame=frame)))
            await socket.open(app=web_server.app, path=cmd['path'], query=cmd['query'].encode(),
                              subprotocols=cmd['subprotocols'], standalone=True)
            self.sockets[key] = socket
            authority_bound = bool(socket.scope.get('hermes.session_authority') or web_server.app.state.session_authority)
            assert socket.scope.get('hermes.forward_shared_token') is True
            return dict(transport='canonical' if authority_bound else 'standalone',
                        credential='session-token', authority_bound=authority_bound)
        return await super().control(cmd)

    async def disconnect(self):
        if hasattr(self, 'release_turn'):
            self.release_turn.set()
        try:
            await super().disconnect()
        finally:
            if self.control_socket:
                await self.control_socket.stop()


if __name__ == '__main__':
    asyncio.run(run(ForwardPeer))
